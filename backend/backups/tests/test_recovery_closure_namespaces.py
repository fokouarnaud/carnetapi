"""Technical closure UUIDs cannot acquire a second business meaning."""
from types import SimpleNamespace
import uuid

from django.test import TestCase
from django.utils.dateparse import parse_datetime
from django.core.files.uploadedfile import SimpleUploadedFile

from backups import models as backups
from backups.tests import test_recovery_catalogue_revisions as catalogue_fixtures
from backups.tests.test_recovery_closed_integration import close_partial
from catalogue.models import Product, ProductPack
from common.models import Activity
from identity.models import Installation, ShopDeviceAccess
from stock.models import StockMovement


class RecoveryClosureNamespaceTests(TestCase, catalogue_fixtures.CatalogueSources):
    setUp = catalogue_fixtures.RecoveryCatalogueRevisionTests.setUp
    authenticate = catalogue_fixtures.RecoveryCatalogueRevisionTests.authenticate
    generation = catalogue_fixtures.RecoveryCatalogueRevisionTests.generation
    record = catalogue_fixtures.RecoveryCatalogueRevisionTests.record
    device = catalogue_fixtures.RecoveryCatalogueRevisionTests.device
    review = catalogue_fixtures.RecoveryCatalogueRevisionTests.review
    payload = catalogue_fixtures.RecoveryCatalogueRevisionTests.payload
    url = catalogue_fixtures.RecoveryCatalogueRevisionTests.url
    capture = catalogue_fixtures.RecoveryCatalogueRevisionTests.capture
    preview = catalogue_fixtures.RecoveryCatalogueRevisionTests.preview
    apply = catalogue_fixtures.RecoveryCatalogueRevisionTests.apply
    before = catalogue_fixtures.RecoveryCatalogueRevisionTests.before

    def next_generation(self):
        backups.RestoreAccessGeneration.objects.filter(current_slot=1).update(current_slot=None)
        self.current = self.generation()
        self.registry = self.record(self.current)
        self.review(self.a, self.owner, "owner", confirmed=True)

    def test_source_uuid_of_an_old_closure_stays_captured_but_cannot_be_integrated_in_a_new_generation(self):
        closure, _ = close_partial(self)
        self.next_generation()
        source = self.product_source(self.before())
        source.operation_id = uuid.UUID(closure["operation_id"])
        source.payload["original_request"]["operation_id"] = closure["operation_id"]
        capture = self.capture(source)
        preview = self.preview(capture)
        self.assertFalse(preview["can_apply"])
        self.assertEqual({item["code"] for item in preview["issues"]}, {"operation_conflict"})
        response, _ = self.apply(capture, preview)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(Activity.objects.filter(id=source.operation_id).exists())
        self.product.refresh_from_db()
        self.assertEqual((self.product.sale_price_fcfa, self.product.version), (250, 1))
        self.assertTrue(backups.RecoveryJournalOperation.objects.filter(id=capture.id).exists())
        self.assertEqual(backups.RecoveryClosure.objects.count(), 1)

    def test_source_uuid_of_a_review_decision_cannot_acquire_a_business_effect(self):
        first = self.capture(self.product_source(self.before()))
        response, command = self.apply(first)
        self.assertEqual(response.status_code, 201, response.content)
        self.product.refresh_from_db()
        source = self.pack_source(self.before(pack=True))
        source.operation_id = uuid.UUID(command["operation_id"])
        source.payload["original_request"]["operation_id"] = command["operation_id"]
        capture = self.capture(source)
        preview = self.preview(capture)
        self.assertFalse(preview["can_apply"])
        self.assertEqual({item["code"] for item in preview["issues"]}, {"operation_conflict"})
        rejected, _ = self.apply(capture, preview)
        self.assertEqual(rejected.status_code, 409, rejected.content)
        self.pack.refresh_from_db(); self.product.refresh_from_db()
        self.assertEqual((self.pack.base_quantity_milli, self.pack.version, self.product.version), (24000, 1, 2))
        self.assertFalse(Activity.objects.filter(id=source.operation_id).exists())
        self.assertTrue(backups.RecoveryJournalOperation.objects.filter(id=capture.id).exists())
        self.assertEqual(backups.RecoveryIntegrationDecision.objects.count(), 1)

    def test_product_creation_cannot_reuse_closure_uuid_or_create_its_opening_stock(self):
        closure, _ = close_partial(self)
        before_counts = (Product.objects.count(), StockMovement.objects.count())
        data = {"id": str(uuid.uuid4()), "operation_id": closure["operation_id"], "name": "Autre produit",
            "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 90,
            "opening": {"quantity_milli": 1000, "unit_cost_fcfa": "30.000000",
                "occurred_at": self.past.isoformat(), "reason": "Ouverture contrôlée"}}
        response = self.client.post(f"/api/v1/shops/{self.a.id}/products/", data, format="json")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual((Product.objects.count(), StockMovement.objects.count()), before_counts)
        self.assertFalse(Product.objects.filter(id=data["id"]).exists())
        self.assertFalse(Activity.objects.filter(id=closure["operation_id"]).exists())

    def test_pack_creation_cannot_reuse_closure_uuid(self):
        closure, _ = close_partial(self)
        data = {"id": str(uuid.uuid4()), "operation_id": closure["operation_id"], "name": "Autre carton",
            "base_quantity_milli": 12000, "sale_price_fcfa": 3000}
        response = self.client.post(f"/api/v1/shops/{self.a.id}/products/{self.product.id}/packs/", data, format="json")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual((ProductPack.objects.count(), StockMovement.objects.count()), (1, 1))
        self.assertFalse(Activity.objects.filter(id=closure["operation_id"]).exists())

    def test_coherent_native_creation_receipts_remain_retryable_after_their_source_is_captured(self):
        for kind in ("product", "pack"):
            with self.subTest(kind=kind):
                if kind == "product":
                    path = f"/api/v1/shops/{self.a.id}/products/"
                    data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Produit connu",
                        "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 90}
                else:
                    path = f"/api/v1/shops/{self.a.id}/products/{self.product.id}/packs/"
                    data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Carton connu",
                        "base_quantity_milli": 12000, "sale_price_fcfa": 3000}
                initial = self.client.post(path, data, format="json")
                self.assertEqual(initial.status_code, 201, initial.content)
                self.capture(SimpleNamespace(operation_id=uuid.UUID(data["operation_id"]), operation_type=kind + "_creation",
                    origin_user_id=self.owner.id, origin_installation_id=self.live.installation_id,
                    occurred_at=parse_datetime(initial.data["created_at"]),
                    payload={"schema_version": 1, "original_request": data, kind: initial.data}))
                retry = self.client.post(path, data, format="json")
                self.assertEqual((retry.status_code, retry.data), (200, initial.data))
                self.assertEqual(Activity.objects.filter(id=data["operation_id"]).count(), 1)

    def test_device_revocation_cannot_reuse_closure_uuid_or_create_a_withdrawal(self):
        closure, _ = close_partial(self)
        target = Installation.objects.create(user=self.owner)
        data = {"operation_id": closure["operation_id"], "installation_id": str(target.id),
            "reason": "Autre appareil réellement retiré"}
        response = self.client.post(f"/api/v1/shops/{self.a.id}/revoke-device/", data, format="json")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(ShopDeviceAccess.objects.filter(shop=self.a, installation=target).exists())
        self.assertFalse(Activity.objects.filter(id=closure["operation_id"]).exists())
        data["operation_id"] = str(uuid.uuid4())
        path = f"/api/v1/shops/{self.a.id}/revoke-device/"
        initial = self.client.post(path, data, format="json")
        self.assertEqual(initial.status_code, 200, initial.content)
        self.capture(SimpleNamespace(operation_id=uuid.UUID(data["operation_id"]), operation_type="device_revocation",
            origin_user_id=self.owner.id, origin_installation_id=self.live.installation_id,
            occurred_at=parse_datetime(initial.data["revoked_at"]),
            payload={"schema_version": 1, "original_request": data, "revocation": initial.data}))
        retry = self.client.post(path, data, format="json")
        self.assertEqual((retry.status_code, retry.data), (200, initial.data))

    def test_native_creations_and_revocation_reserve_the_review_decision_namespace(self):
        first = self.capture(self.product_source(self.before()))
        response, command = self.apply(first)
        self.assertEqual(response.status_code, 201, response.content)
        target = Installation.objects.create(user=self.owner)
        operation_id = command["operation_id"]
        cases = [
            (f"/api/v1/shops/{self.a.id}/products/", {"id": str(uuid.uuid4()), "name": "Autre produit",
                "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 90}),
            (f"/api/v1/shops/{self.a.id}/products/{self.product.id}/packs/", {"id": str(uuid.uuid4()),
                "name": "Autre carton", "base_quantity_milli": 12000, "sale_price_fcfa": 3000}),
            (f"/api/v1/shops/{self.a.id}/revoke-device/", {"installation_id": str(target.id), "reason": "Retrait contrôlé"}),
        ]
        before_counts = (Product.objects.count(), ProductPack.objects.count(), StockMovement.objects.count())
        for path, body in cases:
            with self.subTest(path=path):
                response = self.client.post(path, {**body, "operation_id": operation_id}, format="json")
                self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(ShopDeviceAccess.objects.filter(shop=self.a, installation=target).exists())
        self.assertFalse(Activity.objects.filter(id=operation_id).exists())
        self.assertEqual((Product.objects.count(), ProductPack.objects.count(), StockMovement.objects.count()), before_counts)

    def test_catalogue_csv_confirmation_already_refuses_closure_uuid_before_any_row(self):
        closure, _ = close_partial(self)
        before_counts = (Product.objects.count(), StockMovement.objects.count())
        base = f"/api/v1/shops/{self.a.id}/catalogue-imports/"
        source = b"name,unit_kind,unit_label,sale_price_fcfa\nProduit CSV,piece,piece,100\n"
        preview = self.client.post(base, {"id": str(uuid.uuid4()),
            "file": SimpleUploadedFile("catalogue.csv", source, content_type="text/csv")}, format="multipart")
        self.assertEqual(preview.status_code, 201, preview.content)
        data = {"operation_id": closure["operation_id"], "source_sha256": preview.data["source_sha256"], "reason": "Fichier contrôlé"}
        response = self.client.post(base + str(preview.data["id"]) + "/confirm/", data, format="json")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual((Product.objects.count(), StockMovement.objects.count()), before_counts)
        data["operation_id"] = str(uuid.uuid4())
        initial = self.client.post(base + str(preview.data["id"]) + "/confirm/", data, format="json")
        self.assertEqual(initial.status_code, 200, initial.content)
        retry = self.client.post(base + str(preview.data["id"]) + "/confirm/", data, format="json")
        self.assertEqual((retry.status_code, retry.data), (200, initial.data))
        self.assertEqual(Product.objects.count(), before_counts[0] + 1)
