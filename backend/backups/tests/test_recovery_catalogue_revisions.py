"""Recovered tariffs require the exact preceding catalogue state, never stock reset."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

from django.db import connection, IntegrityError
from django.test import TestCase, SimpleTestCase
from django.utils.dateparse import parse_datetime

from backups import models as backups
from backups.tests import test_recovery_customer_dependencies as fixtures
from catalogue.models import Product, ProductPack
from cash.models import CashMovement
from common.models import Activity
from identity.models import DeviceSession, Membership, User
from stock.models import StockMovement


class CatalogueSources:
    def product_source(self, before):
        operation, actor, device = [uuid.uuid4() for _ in range(3)]
        occurred = datetime(2026, 10, 23, 10, tzinfo=timezone.utc)
        after = {**deepcopy(before), "sale_price_fcfa": 350, "purchase_price_fcfa": None,
                 "version": before["version"] + 1, "updated_at": occurred.isoformat()}
        return SimpleNamespace(operation_type="product_update", operation_id=operation,
            origin_user_id=actor, origin_installation_id=device, occurred_at=occurred,
            payload={"schema_version": 1, "original_request": {"operation_id": str(operation),
                "product_id": before["id"], "base_version": before["version"],
                "sale_price_fcfa": 350, "purchase_price_fcfa": None, "reason": "Tarif contrôlé"},
                "catalogue_revision": {"before": before, "after": after}})

    def pack_source(self, before):
        source = self.product_source(before)
        source.operation_type = "pack_update"
        source.payload["original_request"] = {"operation_id": str(source.operation_id),
            "product_id": before["product_id"], "pack_id": before["id"], "base_version": before["version"],
            "base_quantity_milli": 12000, "sale_price_fcfa": 3000, "reason": "Format contrôlé"}
        source.payload["catalogue_revision"]["after"] = {**deepcopy(before),
            "base_quantity_milli": 12000, "sale_price_fcfa": 3000, "version": before["version"] + 1,
            "product_version": before["product_version"] + 1, "updated_at": source.occurred_at.isoformat(),
            "product_updated_at": source.occurred_at.isoformat()}
        return source


class RecoveryCatalogueRevisionTests(TestCase, CatalogueSources):
    authenticate = fixtures.RecoveryCustomerDependencyTests.authenticate
    generation = fixtures.RecoveryCustomerDependencyTests.generation
    record = fixtures.RecoveryCustomerDependencyTests.record
    device = fixtures.RecoveryCustomerDependencyTests.device
    review = fixtures.RecoveryCustomerDependencyTests.review
    payload = fixtures.RecoveryCustomerDependencyTests.payload
    url = fixtures.RecoveryCustomerDependencyTests.url
    capture = fixtures.RecoveryCustomerDependencyTests.capture
    preview = fixtures.RecoveryCustomerDependencyTests.preview
    apply = fixtures.RecoveryCustomerDependencyTests.apply

    def setUp(self):
        fixtures.RecoveryCustomerDependencyTests.setUp(self)
        self.live = DeviceSession.objects.get(installation__user=self.owner)
        self.past = datetime(2026, 10, 22, 9, tzinfo=timezone.utc)
        self.product = Product.objects.create(shop=self.a, name="Bouteille", unit_kind="piece",
            unit_label="bouteille", sale_price_fcfa=250, purchase_price_fcfa=100)
        Product.objects.filter(id=self.product.id).update(created_at=self.past, updated_at=self.past)
        self.product.refresh_from_db()
        self.pack = ProductPack.objects.create(product=self.product, name="Carton",
            base_quantity_milli=24000, sale_price_fcfa=6000)
        ProductPack.objects.filter(id=self.pack.id).update(created_at=self.past, updated_at=self.past)
        self.pack.refresh_from_db()
        event = Activity.objects.create(shop=self.a, actor=self.owner, action="stock_opened",
            target_id=self.product.id, reason="Stock survivant")
        StockMovement.objects.create(product=self.product, actor=self.owner, operation=event,
            kind="opening", quantity_milli=17000, unit_cost_micros=100123456, occurred_at=self.past,
            reason="Stock survivant")

    def before(self, pack=False):
        item = self.pack if pack else self.product
        fields = ("id", "name", "base_quantity_milli", "sale_price_fcfa", "is_active", "version") if pack else (
            "id", "name", "category_id", "unit_kind", "unit_label", "sale_price_fcfa", "purchase_price_fcfa",
            "stock_threshold_milli", "is_active", "version")
        state = {key: str(getattr(item, key)) if key.endswith("_id") or key == "id" else getattr(item, key) for key in fields}
        if not pack:state["category_id"] = None
        state.update(created_at=item.created_at.isoformat(), updated_at=item.updated_at.isoformat())
        if pack:state.update(product_id=str(self.product.id), product_version=self.product.version,
            product_updated_at=self.product.updated_at.isoformat())
        else:state["shop_id"] = str(self.a.id)
        return state

    def test_lost_price_correction_recovers_once_without_revaluing_or_resetting_surviving_stock(self):
        source = self.product_source(self.before()); capture = self.capture(source)
        preview = self.preview(capture)
        self.assertTrue(preview["can_apply"])
        self.assertEqual(preview["current_state"]["sale_price_fcfa"], 250)
        response, command = self.apply(capture, preview)
        self.assertEqual(response.status_code, 201, response.content)
        self.product.refresh_from_db()
        self.assertEqual((self.product.sale_price_fcfa, self.product.purchase_price_fcfa, self.product.version), (350, None, 2))
        self.assertEqual(self.product.updated_at, source.occurred_at)
        self.assertEqual((StockMovement.objects.count(), CashMovement.objects.count()), (1, 0))
        self.assertEqual((StockMovement.objects.get().quantity_milli, StockMovement.objects.get().unit_cost_micros), (17000, 100123456))
        self.assertEqual(backups.RecoveredOperationOrigin.objects.get().capture_id, capture.id)
        self.assertEqual(self.preview(capture)["outcome"], "already_known")
        retry = self.client.post(self.url(f"operations/{capture.id}/integrate/"), command, format="json")
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(retry.data, response.data)

    def test_lost_pack_change_advances_shared_sale_version_without_touching_base_stock(self):
        capture = self.capture(self.pack_source(self.before(pack=True)))
        response, _ = self.apply(capture)
        self.assertEqual(response.status_code, 201, response.content)
        self.pack.refresh_from_db(); self.product.refresh_from_db()
        self.assertEqual((self.pack.base_quantity_milli, self.pack.sale_price_fcfa, self.pack.version), (12000, 3000, 2))
        self.assertEqual((self.product.version, self.product.sale_price_fcfa), (2, 250))
        self.assertEqual((StockMovement.objects.count(), CashMovement.objects.count()), (1, 0))
        self.assertEqual(self.preview(capture)["outcome"], "already_known")

    def test_real_native_pack_then_product_revisions_recover_the_exact_parent_timestamp_chain(self):
        product_before, pack_before = self.before(), self.before(pack=True)
        pack_body = {"operation_id": str(uuid.uuid4()), "base_version": 1,
            "reason": "Format contrôlé", "base_quantity_milli": 12000, "sale_price_fcfa": 3000}
        base = f"/api/v1/shops/{self.a.id}/products/{self.product.id}/"
        packed = self.client.patch(base + f"packs/{self.pack.id}/", pack_body, format="json")
        self.assertEqual(packed.status_code, 200, packed.content)
        product_body = {"operation_id": str(uuid.uuid4()), "base_version": 2,
            "reason": "Tarif après format", "sale_price_fcfa": 400}
        priced = self.client.patch(base, product_body, format="json")
        self.assertEqual(priced.status_code, 200, priced.content)
        sources = []
        for body, kind, keys in ((pack_body, "pack_update", pack_before.keys()),
                                  (product_body, "product_update", product_before.keys())):
            event = Activity.objects.get(id=body["operation_id"])
            sources.append(SimpleNamespace(operation_type=kind, operation_id=event.id, origin_user_id=self.owner.id,
                origin_installation_id=self.live.installation_id, occurred_at=parse_datetime(event.details["response"]["updated_at"]),
                payload={"schema_version": 1, "original_request": event.details["request"], "catalogue_revision": {
                    "before": {key: event.details["before"][key] for key in keys},
                    "after": {key: event.details["response"][key] for key in keys}}}))
        native_parent_time = sources[0].payload["catalogue_revision"]["after"]["product_updated_at"]
        self.assertEqual(native_parent_time, sources[1].payload["catalogue_revision"]["before"]["updated_at"])
        Product.objects.filter(id=self.product.id).update(version=1, sale_price_fcfa=250, updated_at=self.past)
        ProductPack.objects.filter(id=self.pack.id).update(version=1, base_quantity_milli=24000, sale_price_fcfa=6000,
            updated_at=self.past)
        Activity.objects.filter(id__in=[source.operation_id for source in sources]).delete()
        pack_capture = self.capture(sources[0]); product_capture = self.capture(sources[1], [sources[0].operation_id])
        self.assertFalse(self.preview(product_capture)["can_apply"])
        packed, _ = self.apply(pack_capture); self.assertEqual(packed.status_code, 201, packed.content)
        self.product.refresh_from_db()
        self.assertEqual(self.product.updated_at, parse_datetime(native_parent_time))
        priced, _ = self.apply(product_capture); self.assertEqual(priced.status_code, 201, priced.content)
        self.product.refresh_from_db(); self.pack.refresh_from_db()
        self.assertEqual((self.product.version, self.product.sale_price_fcfa, self.pack.base_quantity_milli), (3, 400, 12000))
        self.assertEqual((StockMovement.objects.count(), CashMovement.objects.count()), (1, 0))

    def test_native_known_revisions_are_recognized_with_explicit_legacy_installation_notice(self):
        body = {"operation_id": str(uuid.uuid4()), "base_version": 1,
            "reason": "Correction native", "sale_price_fcfa": 350, "purchase_price_fcfa": None}
        changed = self.client.patch(f"/api/v1/shops/{self.a.id}/products/{self.product.id}/", body, format="json")
        self.assertEqual(changed.status_code, 200, changed.content)
        event = Activity.objects.get(id=body["operation_id"])
        source = self.product_source(self.before())
        source.operation_id = event.id; source.origin_user_id = self.owner.id; source.origin_installation_id = self.live.installation_id
        source.occurred_at = parse_datetime(changed.data["updated_at"])
        keys = source.payload["catalogue_revision"]["before"].keys()
        source.payload = {"schema_version": 1, "original_request": event.details["request"], "catalogue_revision": {
            "before": {key: event.details["before"][key] for key in keys},
            "after": {key: event.details["response"][key] for key in keys}}}
        capture = self.capture(source); preview = self.preview(capture)
        self.assertEqual(preview["outcome"], "already_known")
        self.assertFalse(preview["origin_installation_verified"])
        self.assertIn("legacy_origin_installation_unknown", {row["code"] for row in preview["notices"]})
        self.assertEqual(self.apply(capture)[0].status_code, 201)
        self.product.refresh_from_db(); self.assertEqual(self.product.version, 2)

    def test_conflicting_price_and_same_version_different_before_state_stay_captured_without_overwrite(self):
        for changes in ({"sale_price_fcfa": 400, "version": 2}, {"name": "Correction survivante"}):
            with self.subTest(changes=changes):
                source = self.product_source(self.before()); capture = self.capture(source)
                Product.objects.filter(id=self.product.id).update(**changes)
                preview = self.preview(capture)
                self.assertFalse(preview["can_apply"])
                self.assertIn("product_revision_conflict", {row["code"] for row in preview["issues"]})
                self.assertEqual(self.apply(capture, preview)[0].status_code, 409)
                self.assertTrue(backups.RecoveryJournalOperation.objects.filter(id=capture.id).exists())
                self.assertFalse(Activity.objects.filter(id=source.operation_id).exists())
                Product.objects.filter(id=self.product.id).update(name="Bouteille", sale_price_fcfa=250, version=1)
        self.assertEqual((StockMovement.objects.count(), CashMovement.objects.count()), (1, 0))

    def test_pack_cannot_overwrite_shared_product_version_and_stale_preview_requires_new_review(self):
        source = self.pack_source(self.before(pack=True)); capture = self.capture(source); old = self.preview(capture)
        Product.objects.filter(id=self.product.id).update(version=2, sale_price_fcfa=300)
        self.assertEqual(self.apply(capture, old)[0].status_code, 409)
        preview = self.preview(capture)
        self.assertFalse(preview["can_apply"])
        self.assertIn("pack_revision_conflict", {row["code"] for row in preview["issues"]})
        self.pack.refresh_from_db()
        self.assertEqual((self.pack.base_quantity_milli, self.pack.version), (24000, 1))

    def test_known_catalogue_operation_keeps_initial_receipt_after_later_confirmed_change(self):
        source = self.product_source(self.before()); capture = self.capture(source)
        first, _ = self.apply(capture); self.assertEqual(first.status_code, 201, first.content)
        Product.objects.filter(id=self.product.id).update(sale_price_fcfa=500, version=3)
        preview = self.preview(capture)
        self.assertEqual(preview["outcome"], "already_known")
        self.assertEqual(preview["current_state"]["sale_price_fcfa"], 500)
        second, _ = self.apply(capture, preview); self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(second.data["business_receipt"]["sale_price_fcfa"], 350)
        self.product.refresh_from_db(); self.assertEqual((self.product.sale_price_fcfa, self.product.version), (500, 3))
        self.assertEqual(Activity.objects.filter(id=source.operation_id).count(), 1)

    def test_foreign_category_and_product_never_leak_or_create_a_cross_shop_relation(self):
        from catalogue.models import Category
        category = Category.objects.create(shop=self.b, name="Catégorie privée B")
        source = self.product_source(self.before())
        source.payload["original_request"]["category_id"] = str(category.id)
        source.payload["catalogue_revision"]["after"]["category_id"] = str(category.id)
        capture = self.capture(source); preview = self.preview(capture)
        self.assertEqual({row["code"] for row in preview["issues"]}, {"missing_category"})
        self.assertNotIn("Catégorie privée B", str(preview))
        self.assertEqual(self.apply(capture, preview)[0].status_code, 409)
        foreign = Product.objects.create(shop=self.b, name="Produit privé B", unit_kind="piece", unit_label="p", sale_price_fcfa=1)
        source = self.product_source(self.before())
        for state in source.payload["catalogue_revision"].values():state["id"] = str(foreign.id)
        source.payload["original_request"]["product_id"] = str(foreign.id)
        capture = self.capture(source); preview = self.preview(capture)
        self.assertEqual({row["code"] for row in preview["issues"]}, {"missing_product"})
        self.assertNotIn("Produit privé B", str(preview))

    def test_collision_origin_mismatch_and_missing_dependencies_remain_explicitly_blocked(self):
        source = self.product_source(self.before()); missing = uuid.uuid4(); capture = self.capture(source, [missing])
        preview = self.preview(capture)
        self.assertEqual({row["code"] for row in preview["issues"]}, {"missing_dependencies"})
        foreign = Activity.objects.create(id=missing, shop=self.b, actor=self.owner, action="foreign", target_id=uuid.uuid4(),
            reason="Privé", details={"response": {"private": "secret"}})
        self.assertFalse(self.preview(capture)["can_apply"])
        foreign.delete()
        Activity.objects.create(id=missing, shop=self.a, actor=self.owner, action="dependency", target_id=uuid.uuid4(),
            reason="Dépendance retrouvée", details={"response": {}})
        self.assertTrue(self.preview(capture)["can_apply"])
        Activity.objects.create(id=source.operation_id, shop=self.b, actor=self.owner, action="private", target_id=uuid.uuid4(),
            reason="Privé", details={"response": {"private": "secret"}})
        preview = self.preview(capture)
        self.assertEqual({row["code"] for row in preview["issues"]}, {"operation_conflict"})
        self.assertNotIn("secret", str(preview))
        self.assertEqual(self.apply(capture, preview)[0].status_code, 409)

    def test_existing_device_with_another_identity_and_fractional_piece_pack_stay_blocked(self):
        source = self.product_source(self.before()); source.origin_installation_id = self.live.installation_id
        capture = self.capture(source); preview = self.preview(capture)
        self.assertEqual({row["code"] for row in preview["issues"]}, {"origin_identity_conflict"})
        self.assertEqual(self.apply(capture, preview)[0].status_code, 409)
        source = self.pack_source(self.before(pack=True))
        source.payload["original_request"]["base_quantity_milli"] = 1500
        source.payload["catalogue_revision"]["after"]["base_quantity_milli"] = 1500
        capture = self.capture(source); preview = self.preview(capture)
        self.assertEqual({row["code"] for row in preview["issues"]}, {"product_unit_conflict"})
        self.assertEqual(self.apply(capture, preview)[0].status_code, 409)
        self.assertEqual((backups.RecoveredOperationOrigin.objects.count(), CashMovement.objects.count()), (0, 0))

    def test_failed_review_rolls_back_catalogue_version_source_audit_and_origin(self):
        capture = self.capture(self.product_source(self.before()))
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER fail_catalogue_review BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected catalogue review failure'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(capture)
        finally:
            with connection.cursor() as cursor:cursor.execute("DROP TRIGGER fail_catalogue_review")
        self.product.refresh_from_db()
        self.assertEqual((self.product.sale_price_fcfa, self.product.version), (250, 1))
        self.assertFalse(Activity.objects.filter(id=capture.operation_id).exists())
        self.assertEqual(backups.RecoveredOperationOrigin.objects.count(), 0)
        self.assertEqual(self.apply(capture)[0].status_code, 201)

    def test_current_responsible_rights_are_checked_even_on_identical_successful_retry(self):
        manager = User.objects.create_user(username="catalogue-recovery-manager")
        member = Membership.objects.create(shop=self.a, user=manager, role="manager")
        self.review(self.a, manager, "manager", membership=member, confirmed=True)
        self.client = self.authenticate(manager)
        capture = self.capture(self.product_source(self.before()))
        response, command = self.apply(capture); self.assertEqual(response.status_code, 201, response.content)
        member.is_active = False; member.save()
        retry = self.client.post(self.url(f"operations/{capture.id}/integrate/"), command, format="json")
        self.assertEqual(retry.status_code, 404, retry.content)


class RecoveryCatalogueSnapshotTests(SimpleTestCase, CatalogueSources):
    def before(self):
        return {"id": str(uuid.uuid4()), "shop_id": str(uuid.uuid4()), "category_id": None, "name": "Bouteille",
            "unit_kind": "piece", "unit_label": "bouteille", "sale_price_fcfa": 250, "purchase_price_fcfa": 100,
            "stock_threshold_milli": None, "is_active": True, "version": 1,
            "created_at": "2026-10-22T09:00:00Z", "updated_at": "2026-10-22T09:00:00Z"}

    def test_undeclared_fields_units_dates_versions_scope_and_numeric_coercions_are_rejected(self):
        from backups.services.recovery_catalogue_snapshots import decode_catalogue_revision, RecoveryCatalogueRevisionInvalid
        for case in ("unit", "name", "stock", "created", "occurred", "version", "bool", "shop", "device", "threshold"):
            with self.subTest(case=case):
                source = self.product_source(self.before()); source.shop_id = uuid.UUID(source.payload["catalogue_revision"]["before"]["shop_id"])
                after = source.payload["catalogue_revision"]["after"]; raw = source.payload["original_request"]
                if case == "unit":after["unit_kind"] = "weight"
                elif case == "name":after["name"] = "Changement caché"
                elif case == "stock":after["stock_validated_milli"] = 100000
                elif case == "created":after["created_at"] = "2026-10-21T09:00:00Z"
                elif case == "occurred":after["updated_at"] = "2026-10-24T10:00:00Z"
                elif case == "version":after["version"] = 3
                elif case == "bool":raw["sale_price_fcfa"] = True
                elif case == "shop":source.shop_id = uuid.uuid4()
                elif case == "device":raw["installation_id"] = str(uuid.uuid4())
                else:raw["stock_threshold_milli"] = after["stock_threshold_milli"] = 500
                with self.assertRaises(RecoveryCatalogueRevisionInvalid):decode_catalogue_revision(source)

    def test_valid_delta_keeps_unknown_cost_exact_zero_price_and_parent_clock_proof(self):
        from backups.services.recovery_catalogue_snapshots import decode_catalogue_revision
        source = self.product_source(self.before()); source.shop_id = uuid.UUID(source.payload["catalogue_revision"]["before"]["shop_id"])
        source.payload["original_request"]["sale_price_fcfa"] = source.payload["catalogue_revision"]["after"]["sale_price_fcfa"] = 0
        decoded = decode_catalogue_revision(source)
        self.assertEqual((decoded.snapshot["after"]["sale_price_fcfa"], decoded.snapshot["after"]["purchase_price_fcfa"]), (0, None))
        self.assertEqual((decoded.snapshot["before"]["purchase_price_fcfa"], decoded.snapshot["after"]["version"]), (100, 2))

    def test_pack_parent_timestamp_must_be_explicit_and_within_the_recorded_source_timeline(self):
        from backups.services.recovery_catalogue_snapshots import decode_catalogue_revision, RecoveryCatalogueRevisionInvalid
        before = {"id": str(uuid.uuid4()), "product_id": str(uuid.uuid4()), "name": "Carton", "version": 1,
            "product_version": 1, "base_quantity_milli": 24000, "sale_price_fcfa": 6000, "is_active": True,
            "created_at": "2026-10-22T09:00:00Z", "updated_at": "2026-10-22T09:00:00Z",
            "product_updated_at": "2026-10-22T09:00:00Z"}
        source = self.pack_source(before); source.origin_recorded_at = datetime(2026, 10, 23, 10, 1, tzinfo=timezone.utc)
        self.assertEqual(decode_catalogue_revision(source).snapshot["after"]["product_version"], 2)
        for case in ("missing", "before_future", "after_before", "after_recorded", "parent_version"):
            with self.subTest(case=case):
                changed = deepcopy(source); revision = changed.payload["catalogue_revision"]
                if case == "missing":revision["after"].pop("product_updated_at")
                elif case == "before_future":revision["before"]["product_updated_at"] = "2026-10-23T10:00:01Z"
                elif case == "after_before":revision["after"]["product_updated_at"] = "2026-10-22T08:00:00Z"
                elif case == "after_recorded":revision["after"]["product_updated_at"] = "2026-10-23T10:02:00Z"
                else:revision["after"]["product_version"] = 3
                with self.assertRaises(RecoveryCatalogueRevisionInvalid):decode_catalogue_revision(changed)
