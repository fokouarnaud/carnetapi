"""Catalogue recovery cannot bypass an unresolved pack-price decision."""
from copy import deepcopy
from types import SimpleNamespace
import uuid

from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from backups import models as backups
from backups.tests import test_recovery_catalogue_revisions as fixtures
from catalogue.models import PackPriceConflict
from catalogue.services.pack_price_conflicts import record_pack_price_conflict
from cash.models import CashMovement
from common.models import Activity
from common.services.errors import OperationConflict
from identity.models import DeviceSession, Installation
from stock.models import StockMovement


class RecoveryPackPriceConflictTests(TestCase, fixtures.CatalogueSources):
    setUp = fixtures.RecoveryCatalogueRevisionTests.setUp
    authenticate = fixtures.RecoveryCatalogueRevisionTests.authenticate
    generation = fixtures.RecoveryCatalogueRevisionTests.generation
    record = fixtures.RecoveryCatalogueRevisionTests.record
    device = fixtures.RecoveryCatalogueRevisionTests.device
    review = fixtures.RecoveryCatalogueRevisionTests.review
    payload = fixtures.RecoveryCatalogueRevisionTests.payload
    url = fixtures.RecoveryCatalogueRevisionTests.url
    capture = fixtures.RecoveryCatalogueRevisionTests.capture
    preview = fixtures.RecoveryCatalogueRevisionTests.preview
    apply = fixtures.RecoveryCatalogueRevisionTests.apply
    before = fixtures.RecoveryCatalogueRevisionTests.before

    def pack_url(self):
        return f"/api/v1/shops/{self.a.id}/products/{self.product.id}/packs/{self.pack.id}/"

    def open_conflict(self):
        self.pack.refresh_from_db()
        base_version, price = self.pack.version, self.pack.sale_price_fcfa
        command = {"operation_id": str(uuid.uuid4()), "base_version": base_version,
                   "sale_price_fcfa": price + 50, "reason": "Tarif natif sur le premier appareil"}
        response = self.client.patch(self.pack_url(), command, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        other = Installation.objects.create(user=self.owner)
        session = DeviceSession.objects.create(installation=other,
            expires_at=timezone.now() + timezone.timedelta(days=1))
        receipt, created = record_pack_price_conflict(
            shop_id=self.a.id, product_id=self.product.id, pack_id=self.pack.id,
            actor=self.owner, installation=other, session_id=session.id, validated_data={
                "operation_id": uuid.uuid4(), "source_change_id": uuid.UUID(command["operation_id"]),
                "base_version": base_version, "proposed_price_fcfa": price + 100,
                "reason": "Proposition concurrente conservée séparément",
            })
        self.assertTrue(created)
        self.pack.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.pack.sale_price_fcfa, price)
        return command, response.data, receipt

    def compatible_source(self, **changes):
        self.pack.refresh_from_db()
        self.product.refresh_from_db()
        before = self.before(pack=True)
        source = self.pack_source(before)
        source.occurred_at = timezone.now()
        source.payload["original_request"] = {
            "operation_id": str(source.operation_id), "product_id": str(self.product.id),
            "pack_id": str(self.pack.id), "base_version": self.pack.version,
            "reason": "Révision du conditionnement conservée", **changes,
        }
        source.payload["catalogue_revision"]["after"] = {
            **deepcopy(before), **changes, "version": self.pack.version + 1,
            "product_version": self.product.version + 1,
            "updated_at": source.occurred_at.isoformat(),
            "product_updated_at": source.occurred_at.isoformat(),
        }
        return source

    def test_new_price_and_factor_revisions_remain_captured_without_any_business_effect(self):
        self.open_conflict()
        for changes in ({"sale_price_fcfa": 6100}, {"base_quantity_milli": 12000},
                        {"sale_price_fcfa": 6000}, {"base_quantity_milli": 24000}):
            with self.subTest(changes=changes):
                source = self.compatible_source(**changes)
                capture = self.capture(source)
                preview = self.preview(capture)
                self.assertFalse(preview["can_apply"], preview)
                self.assertIn("catalogue_pack_price_conflict", {row["code"] for row in preview["issues"]})
                self.assertEqual(self.apply(capture, preview)[0].status_code, 409)
                capture.refresh_from_db()
                self.assertEqual(capture.payload, source.payload)
                self.assertFalse(Activity.objects.filter(id=source.operation_id).exists())
                self.pack.refresh_from_db()
                self.product.refresh_from_db()
                self.assertEqual((self.pack.sale_price_fcfa, self.pack.base_quantity_milli,
                                  self.pack.version, self.product.version), (6000, 24000, 3, 3))
        self.assertEqual((StockMovement.objects.count(), CashMovement.objects.count()), (1, 0))

    def test_direct_persistence_rechecks_price_and_factor_before_any_mutation(self):
        from backups.services.recovery_catalogue_revisions import persist_catalogue_revision
        from backups.services.recovery_catalogue_snapshots import decode_catalogue_revision
        self.open_conflict()
        before = self.before(pack=True)
        for changes in ({"sale_price_fcfa": 6100}, {"base_quantity_milli": 12000}):
            with self.subTest(changes=changes):
                capture = self.capture(self.compatible_source(**changes))
                decoded = decode_catalogue_revision(capture)
                with self.assertRaises(OperationConflict), transaction.atomic():
                    persist_catalogue_revision(shop=self.a, actor=self.owner,
                        installation=self.live.installation, decoded=decoded,
                        origin_capture=capture, recovery_review={"reason": "Pas de contournement direct"})
                self.pack.refresh_from_db()
                self.product.refresh_from_db()
                self.assertEqual(self.before(pack=True), before)
                self.assertFalse(Activity.objects.filter(id=capture.operation_id).exists())
                self.assertFalse(backups.RecoveredOperationOrigin.objects.filter(capture=capture).exists())

    def test_name_revision_is_allowed_and_old_preview_requires_new_confirmation(self):
        self.open_conflict()
        capture = self.capture(self.compatible_source(name="Carton reconnu"))
        old = self.preview(capture)
        self.assertTrue(old["can_apply"], old)
        changed = self.client.patch(self.pack_url(), {"operation_id": str(uuid.uuid4()),
            "base_version": 3, "name": "Nom confirmé ailleurs", "reason": "Autre correction de libellé"}, format="json")
        self.assertEqual(changed.status_code, 200, changed.content)
        self.assertEqual(self.apply(capture, old)[0].status_code, 409)
        self.assertFalse(Activity.objects.filter(id=capture.operation_id).exists())
        fresh = self.capture(self.compatible_source(name="Carton reconnu"))
        response, _command = self.apply(fresh)
        self.assertEqual(response.status_code, 201, response.content)
        self.pack.refresh_from_db()
        self.assertEqual((self.pack.name, self.pack.sale_price_fcfa,
                          self.pack.base_quantity_milli), ("Carton reconnu", 6000, 24000))
        self.assertEqual(PackPriceConflict.objects.filter(pack=self.pack, state="open").count(), 1)
        self.assertEqual((StockMovement.objects.count(), CashMovement.objects.count()), (1, 0))

    def test_known_native_change_and_exact_retry_keep_the_original_receipt(self):
        from backups.services.recovery_catalogue_snapshots import immutable_catalogue_state
        command, original, _conflict = self.open_conflict()
        event = Activity.objects.get(id=command["operation_id"])
        source = SimpleNamespace(operation_type="pack_update", operation_id=event.id,
            origin_user_id=self.owner.id, origin_installation_id=self.live.installation_id,
            occurred_at=parse_datetime(original["updated_at"]), payload={
                "schema_version": 1, "original_request": event.details["request"],
                "catalogue_revision": {
                    "before": immutable_catalogue_state(event.details["before"], pack=True),
                    "after": immutable_catalogue_state(original, pack=True),
                }})
        capture = self.capture(source)
        preview = self.preview(capture)
        self.assertTrue(preview["can_apply"], preview)
        self.assertEqual(preview["outcome"], "already_known")
        response, retry_command = self.apply(capture, preview)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["business_receipt"], original)
        retry = self.client.post(self.url(f"operations/{capture.id}/integrate/"), retry_command, format="json")
        self.assertEqual((retry.status_code, retry.data), (200, response.data))
        native = self.client.patch(self.pack_url(), command, format="json")
        self.assertEqual((native.status_code, native.data), (200, original))
        self.pack.refresh_from_db()
        self.assertEqual((self.pack.sale_price_fcfa, self.pack.base_quantity_milli), (6000, 24000))

    def test_existing_recovery_receipt_survives_a_later_pack_price_conflict(self):
        source = self.compatible_source(sale_price_fcfa=6200)
        capture = self.capture(source)
        response, command = self.apply(capture)
        self.assertEqual(response.status_code, 201, response.content)
        original = deepcopy(response.data)
        self.open_conflict()
        retry = self.client.post(self.url(f"operations/{capture.id}/integrate/"), command, format="json")
        self.assertEqual((retry.status_code, retry.data), (200, original))
        preview = self.preview(capture)
        self.assertTrue(preview["can_apply"], preview)
        self.assertEqual(preview["outcome"], "already_known")
        self.pack.refresh_from_db()
        self.assertEqual((self.pack.sale_price_fcfa, self.pack.base_quantity_milli), (6200, 24000))
