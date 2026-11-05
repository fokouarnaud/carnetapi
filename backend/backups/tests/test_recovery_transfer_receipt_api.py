"""HTTP contracts, live authorization and UUID boundaries for transfer recovery."""
from copy import deepcopy
import json
import uuid

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, FormatChecker

from backups import models as backups
from backups.tests import test_recovery_transfer_receipts as fixtures
from backups.tests.test_recovery_purchase_contract import nullable_schema
from cash.models import CashMovement
from catalogue.models import Product
from common.models import Activity
from identity.models import Membership, Shop, User
from stock.api.transfers import TransferReceiptSerializer
from stock.models import StockMovement, StockTransfer, StockTransferReceipt


class RecoveryTransferReceiptApiTests(TestCase):
    setUp = fixtures.RecoveryTransferReceiptTests.setUp
    authenticate = fixtures.RecoveryTransferReceiptTests.authenticate
    generation = fixtures.RecoveryTransferReceiptTests.generation
    record = fixtures.RecoveryTransferReceiptTests.record
    device = fixtures.RecoveryTransferReceiptTests.device
    review = fixtures.RecoveryTransferReceiptTests.review
    payload = fixtures.RecoveryTransferReceiptTests.payload
    url = fixtures.RecoveryTransferReceiptTests.url
    capture = fixtures.RecoveryTransferReceiptTests.capture
    preview = fixtures.RecoveryTransferReceiptTests.preview
    apply = fixtures.RecoveryTransferReceiptTests.apply
    transfer_url = fixtures.RecoveryTransferReceiptTests.transfer_url
    native_receipt_source = fixtures.RecoveryTransferReceiptTests.native_receipt_source
    stock = fixtures.RecoveryTransferReceiptTests.stock

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def check_schema(self, response, ending, method="get", *, transfer=False):
        path = next(path for path in self.schema["paths"]
                    if path.endswith(ending) and (not transfer or "/transfers/" in path))
        rule = self.schema["paths"][path][method]["responses"][str(response.status_code)]["content"]["application/json"]["schema"]
        validator = Draft4Validator(nullable_schema({**rule, "components": self.schema["components"]}),
                                    format_checker=FormatChecker())
        errors = [(list(error.absolute_path), error.message)
                  for error in validator.iter_errors(json.loads(response.content))]
        self.assertEqual(errors, [])

    def test_preview_apply_retry_and_two_explicit_destinations_match_openapi(self):
        first = self.native_receipt_source()
        capture = self.capture(first)
        response = self.client.get(self.url(f"operations/{capture.id}/integration-preview/"))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.data["operation_type"], response.data["can_apply"]), ("transfer_receipt", True))
        self.assertEqual(response.data["snapshot"]["lines"], first.payload["receipt"]["lines"])
        self.check_schema(response, "/integration-preview/")
        integrated, command = self.apply(capture, response.data)
        self.assertEqual(integrated.status_code, 201, integrated.content)
        self.check_schema(integrated, "/integrate/", "post")
        replay = self.client.post(self.url(f"operations/{capture.id}/integrate/"), command, format="json")
        self.assertEqual((replay.status_code, replay.data), (200, integrated.data))
        self.check_schema(replay, "/integrate/", "post")
        known = self.client.get(self.url(f"operations/{capture.id}/integration-preview/"))
        self.assertEqual(known.data["outcome"], "already_known")
        self.check_schema(known, "/integration-preview/")
        second = self.native_receipt_source(quantity=2000, product=self.second_product)
        following = self.capture(second, [first.operation_id])
        applied, _ = self.apply(following)
        self.assertEqual(applied.status_code, 201, applied.content)
        self.assertEqual((self.stock(self.product), self.stock(self.second_product), self.stock(self.source_product)),
                         ("3000", "2000", "5000"))
        receipt_url = self.transfer_url(suffix=self.transfer["id"] + "/receipts/")
        listing = self.client.get(receipt_url)
        self.assertEqual((listing.status_code, listing.data["count"]), (200, 2), listing.content)
        self.check_schema(listing, "/receipts/", transfer=True)
        rows = [line for receipt in listing.data["results"] for line in receipt["lines"]]
        self.assertEqual({row["product_id"] for row in rows}, {str(self.product.id), str(self.second_product.id)})
        self.assertEqual(sum(int(row["total_cost_nanos_fcfa"]) for row in rows),
                         int(self.transfer["lines"][0]["cost"]["total_cost_nanos_fcfa"]))
        progress = self.client.get(self.transfer_url(suffix=self.transfer["id"] + "/"))
        self.assertEqual(progress.data["lines"][0]["in_transit_milli"], "0")
        self.assertFalse(CashMovement.objects.exists())

    def test_native_reads_preserve_source_identity_after_a_different_responsible_replays(self):
        source = self.native_receipt_source()
        capture = self.capture(source)
        manager = User.objects.create_user(username="transfer-api-destination-manager")
        membership = Membership.objects.create(user=manager, shop=self.a, role="manager")
        self.review(self.a, manager, "manager", membership=membership, confirmed=True)
        self.client = self.authenticate(manager)
        self.assertEqual(self.client.get(self.url(f"operations/{capture.id}/integration-preview/", shop=self.b)).status_code, 404)
        integrated, _ = self.apply(capture)
        self.assertEqual(integrated.status_code, 201, integrated.content)
        public = integrated.data["business_receipt"]
        self.assertEqual((public["actor_id"], public["installation_id"]),
                         (str(source.origin_user_id), str(source.origin_installation_id)))
        self.assertEqual(integrated.data["reviewed_by"], str(manager.id))
        self.assertEqual(Activity.objects.get(id=source.operation_id).actor_id, manager.id)
        rows = self.client.get(self.transfer_url(suffix=self.transfer["id"] + "/receipts/"))
        self.assertEqual(rows.status_code, 200, rows.content)
        self.assertEqual((rows.data["results"][0]["actor_id"], rows.data["results"][0]["installation_id"]),
                         (public["actor_id"], public["installation_id"]))
        self.check_schema(rows, "/receipts/", transfer=True)
        stored = StockTransferReceipt.objects.select_related("operation").prefetch_related("lines").get()
        with CaptureQueriesContext(connection) as queries:
            single = TransferReceiptSerializer(stored).data
        self.assertLessEqual(len(queries), 1, "Unit receipt serialization must use a bounded origin lookup.")
        self.assertEqual((single["actor_id"], single["installation_id"]), (public["actor_id"], public["installation_id"]))
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.b.id}/products/").status_code, 404)

    def test_live_role_precedes_parsing_and_withdrawn_confirmation_precedes_exact_retry(self):
        capture = self.capture(self.native_receipt_source())
        seller = User.objects.create_user(username="transfer-api-seller")
        membership = Membership.objects.create(user=seller, shop=self.a)
        self.review(self.a, seller, "seller", membership=membership, confirmed=True)
        seller_api = self.authenticate(seller)
        target = self.url(f"operations/{capture.id}/integrate/")
        denied = seller_api.generic("POST", target, b"not-json", content_type="application/json")
        self.assertEqual(denied.status_code, 403, denied.content)
        self.assertEqual(seller_api.get(self.url(f"operations/{capture.id}/integration-preview/")).status_code, 403)
        self.assertEqual(seller_api.get(self.transfer_url(suffix=self.transfer["id"] + "/receipts/")).status_code, 403)
        applied, command = self.apply(capture)
        self.assertEqual(applied.status_code, 201, applied.content)
        review = backups.RestoreAccessReview.objects.get(generation=self.current, shop=self.a, user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review, operation_id=uuid.uuid4(), requested_by=self.technical,
            previous_version=1, version=2, state="refused", reason="Confirmation actuelle retirée")
        self.assertEqual(self.client.get(self.url(f"operations/{capture.id}/integration-preview/")).status_code, 404)
        self.assertEqual(self.client.post(target, command, format="json").status_code, 404)
        self.assertEqual(self.client.get(self.transfer_url(suffix=self.transfer["id"] + "/receipts/")).status_code, 404)
        self.assertEqual(StockTransferReceipt.objects.count(), 1)

    def test_contradictory_archived_destination_never_projects_receipt_identities(self):
        self.native_receipt_source(keep=True)
        foreign = Shop.objects.create(owner=self.owner, name="Destination incohérente de l'archive")
        StockTransfer.objects.filter(id=self.transfer["id"]).update(destination_shop=foreign)
        response = self.client.get(self.transfer_url(self.b, self.transfer["id"] + "/receipts/"))
        self.assertEqual((response.status_code, response.data["code"]), (409, "origin_reference_conflict"))
        self.assertNotIn(str(self.owner.id), response.content.decode("utf-8"))
        from common.services.origins import OriginReferenceConflict
        receipt = StockTransferReceipt.objects.select_related("operation").prefetch_related("lines").get()
        with self.assertRaises(OriginReferenceConflict):
            TransferReceiptSerializer(receipt).data

    def test_changed_token_version_review_and_native_operation_type_cannot_add_effects(self):
        source = self.native_receipt_source()
        capture = self.capture(source)
        preview = self.preview(capture)
        for changes in ({"preview_token": "0" * 64}, {"previous_version": preview["version"] + 1},
                        {"operation_id": str(source.operation_id)}):
            with self.subTest(changes=changes):
                refused, _ = self.apply(capture, preview, **changes)
                self.assertEqual(refused.status_code, 409, refused.content)
                self.assertFalse(StockTransferReceipt.objects.exists())
        accepted, command = self.apply(capture, preview)
        self.assertEqual(accepted.status_code, 201, accepted.content)
        target = self.url(f"operations/{capture.id}/integrate/")
        self.assertEqual(self.client.post(target, {**command, "reason": "Contenu différent"}, format="json").status_code, 409)
        replay = self.client.post(target, command, format="json")
        self.assertEqual((replay.status_code, replay.data), (200, accepted.data))
        departure = deepcopy(self.departure)
        departure.update(id=str(uuid.uuid4()), operation_id=str(source.operation_id), destination_shop_id=str(self.b.id))
        departure["lines"] = [{"id": str(uuid.uuid4()), "product_id": str(self.product.id),
                               "product_version": self.product.version, "quantity_milli": 1000}]
        wrong_type = self.client.post(self.transfer_url(), departure, format="json")
        self.assertEqual(wrong_type.status_code, 409, wrong_type.content)
        self.assertEqual((StockTransfer.objects.count(), StockTransferReceipt.objects.count(),
                          StockMovement.objects.filter(kind="transfer_in").count()), (1, 1, 1))

    def test_foreign_destination_stays_blocked_without_loading_its_current_private_name(self):
        source = self.native_receipt_source()
        private_name = "NOM PRIVE B NON PRESENT DANS LA COPIE"
        Product.objects.filter(id=self.source_product.id).update(name=private_name)
        for row in (source.payload["original_request"]["lines"][0], source.payload["receipt"]["lines"][0]):
            row["product_id"] = str(self.source_product.id)
        capture = self.capture(source)
        response = self.client.get(self.url(f"operations/{capture.id}/integration-preview/"))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.data["can_apply"])
        self.assertNotIn(private_name, response.content.decode("utf-8"))
        self.check_schema(response, "/integration-preview/")
        denied, _ = self.apply(capture, response.data)
        self.assertEqual(denied.status_code, 409, denied.content)
        self.assertEqual(self.stock(self.product), "0")
        self.assertFalse(StockTransferReceipt.objects.exists())

    def test_a_new_physical_arrival_invalidates_the_old_preview_without_rewriting_before(self):
        source = self.native_receipt_source()
        capture = self.capture(source)
        initial = self.preview(capture)
        self.native_receipt_source(keep=True, quantity=1000, product=self.second_product)
        refused, _ = self.apply(capture, initial)
        self.assertEqual(refused.status_code, 409, refused.content)
        current = self.client.get(self.url(f"operations/{capture.id}/integration-preview/"))
        self.assertEqual(current.status_code, 200, current.content)
        self.assertNotEqual(current.data["preview_token"], initial["preview_token"])
        self.assertFalse(current.data["can_apply"])
        self.assertEqual(current.data["snapshot"]["lines"][0]["received_before_milli"], "0")
        self.assertEqual(current.data["snapshot"]["lines"][0]["quantity_milli"], 3000)
        self.check_schema(current, "/integration-preview/")
        self.assertFalse(StockTransferReceipt.objects.filter(id=source.payload["receipt"]["id"]).exists())
        self.assertEqual((self.stock(self.product), self.stock(self.second_product)), ("0", "1000"))

    def test_invalid_physical_receipt_is_typed_and_localized_without_erasing_the_copy(self):
        source = self.native_receipt_source()
        source.payload["receipt"]["lines"][0]["quantity_milli"] = True
        capture = self.capture(source)
        for language, detail in (
            ("en", "Recovery physical transfer receipt snapshot is inconsistent."),
            ("fr", "Les données figées de la réception physique du transfert récupérée sont incohérentes."),
        ):
            with self.subTest(language=language):
                response = self.client.get(self.url(f"operations/{capture.id}/integration-preview/"),
                                           HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual((response.status_code, response.data["code"], str(response.data["detail"])),
                                 (400, "recovery_transfer_receipt_invalid", detail))
                self.check_schema(response, "/integration-preview/")
        capture.refresh_from_db()
        self.assertEqual(capture.payload, source.payload)
        self.assertFalse(StockTransferReceipt.objects.exists())
        self.assertFalse(Activity.objects.filter(id=source.operation_id).exists())
