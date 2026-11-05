"""A received product-price conflict preserves proposals and the last common price."""
from datetime import timedelta
from copy import deepcopy
import uuid

from django.db import DatabaseError, connection, transaction
from django.http import Http404
from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError
from common.services.errors import OperationConflict

from catalogue.api.catalogue_serializers import UpdateProductSerializer
from catalogue.models import Product
from catalogue.services.catalogue_services import update_product
from common.models import Activity
from identity.models import User, Shop, Membership, Installation, DeviceSession
from stock.models import StockMovement
from cash.models import CashMovement
from sales.api.sale_serializers import CreateSaleSerializer
from sales.services.sale_services import create_sale
from sales.models import SaleLine


class ProductPriceConflictTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="price-owner")
        self.manager = User.objects.create_user(username="price-manager")
        self.shop = Shop.objects.create(owner=self.owner, name="A")
        self.other = Shop.objects.create(owner=self.owner, name="B")
        self.membership = Membership.objects.create(shop=self.shop, user=self.manager, role="manager")
        self.a_install = Installation.objects.create(user=self.owner)
        self.b_install = Installation.objects.create(user=self.manager)
        self.a_session = DeviceSession.objects.create(installation=self.a_install, expires_at=timezone.now() + timedelta(days=1))
        self.b_session = DeviceSession.objects.create(installation=self.b_install, expires_at=timezone.now() + timedelta(days=1))
        self.product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce",
            sale_price_fcfa=1000, purchase_price_fcfa=600)
        opening = Activity.objects.create(shop=self.shop, actor=self.owner, action="stock_opened", target_id=self.product.id, reason="Ouverture")
        StockMovement.objects.create(product=self.product, actor=self.owner, operation=opening, kind="opening", quantity_milli=10000,
            unit_cost_micros=600000000, occurred_at=timezone.now(), reason="Ouverture")
        request = UpdateProductSerializer(data={"operation_id": str(uuid.uuid4()), "base_version": 1,
            "sale_price_fcfa": 1100, "reason": "Tarif confirmé sur A"})
        request.is_valid(raise_exception=True)
        update_product(self.shop.id, self.product.id, self.owner, self.a_install, request.validated_data, self.a_session.id)
        self.source = Activity.objects.get(id=request.validated_data["operation_id"])

    def record(self, data=None):
        from catalogue.services.price_conflicts import record_price_conflict
        return record_price_conflict(self.shop.id, self.product.id, self.manager, self.b_install,
            data or self.command(), self.b_session.id)

    def command(self, **values):
        return {"operation_id": str(uuid.uuid4()), "source_change_id": str(self.source.id), "base_version": 1,
            "proposed_price_fcfa": 1200, "reason": "Proposition concurrente de B", **values}

    def preview(self, conflict_id):
        from catalogue.services.price_conflicts import preview_price_conflict
        return preview_price_conflict(self.shop.id, self.product.id, conflict_id, self.manager, self.b_install, self.b_session.id)

    def resolve(self, conflict_id, data):
        from catalogue.services.price_conflicts import resolve_price_conflict
        return resolve_price_conflict(self.shop.id, self.product.id, conflict_id, self.manager, self.b_install, data, self.b_session.id)

    def test_received_conflict_restores_common_price_without_repricing_a_billed_sale_or_changing_money_or_stock(self):
        incoming = CreateSaleSerializer(data={"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 2, "quantity_milli": 1000}],
            "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1100}]})
        incoming.is_valid(raise_exception=True)
        create_sale(self.shop.id, self.owner, self.a_install, incoming.validated_data, self.a_session.id)
        before = (StockMovement.objects.count(), CashMovement.objects.count())
        receipt, created = self.record()
        self.assertTrue(created)
        self.assertEqual((receipt["state"], receipt["version"], receipt["common_price_fcfa"], receipt["product_version"]), ("open", 1, 1000, 3))
        self.assertEqual(sorted(row["proposed_price_fcfa"] for row in receipt["proposals"]), [1100, 1200])
        native = next(row for row in receipt["proposals"] if row["operation_id"] == str(self.source.id))
        self.assertEqual(native["actor_id"], str(self.owner.id))
        self.assertIsNone(native["installation_id"])
        self.product.refresh_from_db(); self.source.refresh_from_db()
        self.assertEqual((self.product.sale_price_fcfa, self.product.version), (1000, 3))
        self.assertEqual((StockMovement.objects.count(), CashMovement.objects.count()), before)
        self.assertEqual((SaleLine.objects.get().unit_price_fcfa, SaleLine.objects.get().net_fcfa), (1100, 1100))
        self.assertEqual(self.source.details["response"]["sale_price_fcfa"], 1100)
        self.assertNotIn("purchase_price_fcfa", str(receipt))

    def test_explicit_resolution_selects_a_preserved_proposal_and_keeps_exact_retries(self):
        registration = self.command()
        conflict, _ = self.record(registration)
        proposal = next(row for row in conflict["proposals"] if row["proposed_price_fcfa"] == 1200)
        preview = self.preview(conflict["id"])
        self.assertTrue(preview["can_resolve"])
        data = {"operation_id": str(uuid.uuid4()), "preview_token": preview["preview_token"],
            "proposal_id": proposal["id"], "reason": "Tarif 1200 confirmé après examen"}
        receipt, created = self.resolve(conflict["id"], data)
        self.assertTrue(created)
        self.assertEqual((receipt["state"], receipt["version"], receipt["product_version"]), ("resolved", 2, 4))
        self.assertEqual((receipt["resolution"]["price_fcfa"], receipt["resolution"]["proposal_id"]), (1200, proposal["id"]))
        self.product.refresh_from_db(); self.assertEqual((self.product.sale_price_fcfa, self.product.version), (1200, 4))
        retry, created = self.resolve(conflict["id"], data)
        self.assertFalse(created)
        self.assertEqual(retry, receipt)
        self.assertEqual(self.record(registration), (conflict, False))
        self.assertEqual((StockMovement.objects.count(), CashMovement.objects.count()), (1, 0))

    def test_extra_proposal_revises_only_the_conflict_and_original_receipt_stays_exact(self):
        data = self.command()
        first, _ = self.record(data)
        old_preview = self.preview(first["id"])
        second, _ = self.record(self.command(proposed_price_fcfa=1300))
        self.assertEqual((second["id"], second["version"], second["product_version"]), (first["id"], 2, 3))
        self.assertEqual(sorted(row["proposed_price_fcfa"] for row in second["proposals"]), [1100, 1200, 1300])
        self.assertEqual(self.record(data), (first, False))
        with self.assertRaises(OperationConflict):
            self.resolve(first["id"], {"operation_id": str(uuid.uuid4()), "proposal_id": None,
                "preview_token": old_preview["preview_token"], "reason": "Aperçu devenu ancien"})

    def test_common_price_is_an_explicit_resolution_and_cannot_be_overridden_by_a_client_amount(self):
        conflict, _ = self.record()
        preview = self.preview(conflict["id"])
        data = {"operation_id": str(uuid.uuid4()), "proposal_id": None,
            "preview_token": preview["preview_token"], "reason": "Conserver le tarif commun"}
        with self.assertRaises(ValidationError):
            self.resolve(conflict["id"], {**data, "price_fcfa": 999})
        receipt, _ = self.resolve(conflict["id"], data)
        self.assertEqual((receipt["resolution"]["price_fcfa"], receipt["resolution"]["proposal_id"]), (1000, None))
        self.assertFalse(self.preview(conflict["id"])["can_resolve"])
        with self.assertRaises(OperationConflict):
            self.record(self.command())

    def test_source_must_be_exact_price_transition_for_the_requested_base_and_product(self):
        original = deepcopy(self.source.details)
        mutations = [
            lambda d: d["request"].update(base_version=True),
            lambda d: d["before"].update(version=True),
            lambda d: d["after"].update(version=3),
            lambda d: d["before"].update(sale_price_fcfa=1000.5),
            lambda d: d["request"].pop("sale_price_fcfa"),
            lambda d: d["after"].update(shop_id=str(self.other.id)),
            lambda d: d["request"].update(product_id=str(uuid.uuid4())),
            lambda d: d["response"].update(name="Réponse contradictoire"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                details = deepcopy(original)
                mutate(details)
                Activity.objects.filter(pk=self.source.id).update(details=details)
                with self.assertRaises(OperationConflict):
                    self.record()
        Activity.objects.filter(pk=self.source.id).update(details=original)
        for field, value in [("base_version", 2), ("source_change_id", str(uuid.uuid4()))]:
            with self.subTest(field=field), self.assertRaises((OperationConflict, Http404)):
                self.record(self.command(**{field: value}))
        self.product.refresh_from_db()
        self.assertEqual((self.product.sale_price_fcfa, self.product.version), (1100, 2))

    def test_an_old_source_is_refused_even_if_the_current_price_still_matches(self):
        Product.objects.filter(pk=self.product.id).update(version=3)
        with self.assertRaises(OperationConflict):
            self.record()
        self.assertEqual(Activity.objects.filter(action="catalogue_price_conflict_recorded").count(), 0)

    def test_conflict_and_resolution_replays_recheck_current_authority(self):
        data = self.command()
        conflict, _ = self.record(data)
        preview = self.preview(conflict["id"])
        decision = {"operation_id": str(uuid.uuid4()), "proposal_id": None,
            "preview_token": preview["preview_token"], "reason": "Décision autorisée"}
        self.resolve(conflict["id"], decision)
        self.membership.role = "seller"
        self.membership.save(update_fields=["role"])
        for call in (lambda: self.record(data), lambda: self.resolve(conflict["id"], decision)):
            with self.subTest(call=call), self.assertRaises(PermissionDenied):
                call()
        self.b_session.revoked_at = timezone.now()
        self.b_session.save(update_fields=["revoked_at"])
        with self.assertRaises(AuthenticationFailed):
            self.record(data)

    def test_preview_is_bound_to_the_reviewing_actor_installation_session_and_current_product_version(self):
        from catalogue.services.price_conflicts import resolve_price_conflict
        conflict, _ = self.record()
        preview = self.preview(conflict["id"])
        decision = {"operation_id": str(uuid.uuid4()), "proposal_id": None,
            "preview_token": preview["preview_token"], "reason": "Décision"}
        with self.assertRaises(OperationConflict):
            resolve_price_conflict(self.shop.id, self.product.id, conflict["id"], self.owner,
                self.a_install, decision, self.a_session.id)
        another = DeviceSession.objects.create(installation=self.b_install, expires_at=timezone.now()+timedelta(days=1))
        with self.assertRaises(OperationConflict):
            resolve_price_conflict(self.shop.id, self.product.id, conflict["id"], self.manager,
                self.b_install, decision, another.id)
        Product.objects.filter(pk=self.product.id).update(version=4)
        with self.assertRaises(OperationConflict):
            self.resolve(conflict["id"], decision)
        decision["preview_token"] = self.preview(conflict["id"])["preview_token"]
        receipt, _ = self.resolve(conflict["id"], decision)
        self.assertEqual(receipt["product_version"], 5)

    def test_a_proposal_outside_the_conflict_or_a_tampered_token_cannot_set_a_price(self):
        conflict, _ = self.record()
        preview = self.preview(conflict["id"])
        data = {"operation_id": str(uuid.uuid4()), "proposal_id": str(uuid.uuid4()),
            "preview_token": preview["preview_token"], "reason": "Décision"}
        with self.assertRaises(OperationConflict):
            self.resolve(conflict["id"], data)
        with self.assertRaises(OperationConflict):
            self.resolve(conflict["id"], {**data, "proposal_id": None, "preview_token": data["preview_token"]+"x"})

    def test_proposed_money_is_an_exact_nonnegative_int64(self):
        for price in (True, 1200.0, "1200", -1, 9223372036854775808):
            with self.subTest(price=price), self.assertRaises(ValidationError):
                self.record(self.command(proposed_price_fcfa=price))
        conflict, _ = self.record(self.command(proposed_price_fcfa=0))
        self.assertIn(0, [row["proposed_price_fcfa"] for row in conflict["proposals"]])

    def test_proposals_resolutions_and_source_evidence_are_immutable_even_through_queryset_updates(self):
        from catalogue.models import ProductPriceConflict, ProductPriceProposal, ProductPriceResolution
        conflict, _ = self.record()
        preview = self.preview(conflict["id"])
        self.resolve(conflict["id"], {"operation_id": str(uuid.uuid4()), "proposal_id": None,
            "preview_token": preview["preview_token"], "reason": "Décision conservée"})
        mutations = [
            lambda: ProductPriceProposal.objects.filter(conflict_id=conflict["id"]).update(proposed_price_fcfa=999),
            lambda: ProductPriceResolution.objects.filter(conflict_id=conflict["id"]).update(price_fcfa=999),
            lambda: ProductPriceConflict.objects.filter(pk=conflict["id"]).update(common_price_fcfa=999),
            lambda: ProductPriceConflict.objects.filter(pk=conflict["id"]).update(source_snapshot={}),
            lambda: ProductPriceConflict.objects.filter(pk=conflict["id"]).update(state="open"),
            lambda: ProductPriceProposal.objects.filter(conflict_id=conflict["id"]).delete(),
            lambda: ProductPriceResolution.objects.filter(conflict_id=conflict["id"]).delete(),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate), self.assertRaises(DatabaseError), transaction.atomic():
                mutate()

    def test_failed_resolution_write_rolls_back_price_revision_and_audit_together(self):
        conflict, _ = self.record()
        preview = self.preview(conflict["id"])
        operation_id = str(uuid.uuid4())
        with connection.cursor() as cursor:
            cursor.execute("CREATE TRIGGER reject_price_resolution BEFORE INSERT ON catalogue_productpriceresolution BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        try:
            with self.assertRaises(DatabaseError), transaction.atomic():
                self.resolve(conflict["id"], {"operation_id": operation_id, "proposal_id": None,
                    "preview_token": preview["preview_token"], "reason": "Décision"})
        finally:
            with connection.cursor() as cursor:
                cursor.execute("DROP TRIGGER reject_price_resolution")
        self.product.refresh_from_db()
        self.assertEqual((self.product.sale_price_fcfa, self.product.version), (1000, 3))
        self.assertFalse(Activity.objects.filter(id=operation_id).exists())

    def attach_recovered_source(self, *, shop=None):
        from backups import models as backups
        shop = shop or self.shop
        generation = backups.RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(),
            backup_id=uuid.uuid4(), initiated_by=self.owner.id)
        session = backups.RestoreSession.objects.create(generation=generation)
        deposit = backups.RecoveryJournalDeposit.objects.create(session=session, shop=shop,
            operation_id=uuid.uuid4(), submitted_by=self.owner, submitted_installation=self.a_install,
            reason="Journal conservé", format_version=1, content_sha256="a"*64, receipt={})
        capture = backups.RecoveryJournalOperation.objects.create(session=session, shop=shop, first_deposit=deposit,
            operation_id=self.source.id, origin_user_id=uuid.uuid4(), origin_installation_id=uuid.uuid4(),
            operation_type="product_update", occurred_at=timezone.now(), origin_recorded_at=timezone.now(),
            payload={}, dependencies=[], content_sha256="b"*64)
        backups.RecoveredOperationOrigin.objects.create(operation=self.source, capture=capture)
        return capture

    def test_recovered_native_proposal_keeps_the_historical_author_and_device_without_creating_access(self):
        capture = self.attach_recovered_source()
        receipt, _ = self.record()
        native = next(row for row in receipt["proposals"] if row["operation_id"] == str(self.source.id))
        self.assertEqual((native["actor_id"], native["installation_id"]),
            (str(capture.origin_user_id), str(capture.origin_installation_id)))
        self.assertFalse(User.objects.filter(id=capture.origin_user_id).exists())
        self.assertFalse(Installation.objects.filter(id=capture.origin_installation_id).exists())

    def test_contradictory_recovery_origin_is_refused_before_any_conflict_or_fallback(self):
        self.attach_recovered_source(shop=self.other)
        with self.assertRaises(OperationConflict):
            self.record()
        self.product.refresh_from_db()
        self.assertEqual((self.product.sale_price_fcfa, self.product.version), (1100, 2))

    def test_first_record_failure_rolls_back_the_fallback_source_proposal_and_operation(self):
        from catalogue.models import ProductPriceConflict, ProductPriceProposal
        command = self.command()
        with connection.cursor() as cursor:
            cursor.execute("CREATE TRIGGER reject_received_proposal BEFORE INSERT ON catalogue_productpriceproposal WHEN NEW.is_native=0 BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        try:
            with self.assertRaises(DatabaseError), transaction.atomic():
                self.record(command)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("DROP TRIGGER reject_received_proposal")
        self.product.refresh_from_db()
        self.assertEqual((self.product.sale_price_fcfa, self.product.version), (1100, 2))
        self.assertEqual((ProductPriceConflict.objects.count(), ProductPriceProposal.objects.count()), (0, 0))
        self.assertFalse(Activity.objects.filter(id=command["operation_id"]).exists())

    def test_new_operations_and_foreign_object_identifiers_never_replay_an_unrelated_receipt(self):
        from catalogue.services.price_conflicts import record_price_conflict, preview_price_conflict
        conflict, _ = self.record()
        with self.assertRaises(OperationConflict):
            self.record(self.command(operation_id=str(self.source.id)))
        with self.assertRaises(OperationConflict):
            self.record(self.command(operation_id=conflict["proposals"][-1]["operation_id"], proposed_price_fcfa=1300))
        with self.assertRaises(Http404):
            preview_price_conflict(self.other.id, self.product.id, conflict["id"], self.owner,
                self.a_install, self.a_session.id)
        with self.assertRaises(Http404):
            record_price_conflict(self.other.id, self.product.id, self.owner, self.a_install,
                self.command(), self.a_session.id)

    def test_a_large_exact_proposal_can_be_resolved_without_float_rounding(self):
        conflict, _ = self.record(self.command(proposed_price_fcfa=9223372036854775807))
        proposal = next(row for row in conflict["proposals"] if row["proposed_price_fcfa"] == 9223372036854775807)
        preview = self.preview(conflict["id"])
        self.assertLessEqual(len(preview["preview_token"]), 512)
        receipt, _ = self.resolve(conflict["id"], {"operation_id": str(uuid.uuid4()), "proposal_id": proposal["id"],
            "preview_token": preview["preview_token"], "reason": "Montant entier exact"})
        self.assertEqual(receipt["resolution"]["price_fcfa"], 9223372036854775807)

    def test_validation_messages_are_available_in_french_and_english(self):
        from django.utils.translation import override
        for language, expected in (("fr", "Un entier exact dans les limites prises en charge est requis."),
                ("en", "An exact integer within the supported range is required.")):
            with self.subTest(language=language), override(language):
                with self.assertRaises(ValidationError) as caught:
                    self.record(self.command(proposed_price_fcfa=True))
                self.assertEqual(str(caught.exception.detail[0]), expected)
