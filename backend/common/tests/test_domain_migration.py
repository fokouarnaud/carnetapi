"""The app split must preserve populated tables, access grants and audit links."""
import hashlib
import json
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.db import connection, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone


PREVIOUS = [("core", "0080_alter_membership_options_alter_shop_options_and_more")]
DOMAIN_BRIDGE = [(label, "0001_domain_state") for label in (
    "identity", "catalogue", "stock", "sales", "customers", "purchasing", "cash")]


class MigrationHistoryTests(SimpleTestCase):
    def test_original_migrations_are_preserved_byte_for_byte(self):
        root = Path(__file__).resolve().parents[3]
        expected = json.loads((root / "analysis/backend-migration-history.json").read_text(encoding="utf-8"))
        self.assertEqual(len(expected), 80)
        for name, digest in expected.items():
            with self.subTest(migration=name):
                self.assertEqual(hashlib.sha256((settings.BASE_DIR / "core/migrations" / name).read_bytes()).hexdigest(), digest)


class DomainMigrationTests(TransactionTestCase):
    def snapshot(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE tbl_name LIKE 'core_%' ORDER BY type, name")
            schema = cursor.fetchall()
            rows = {}
            for kind, table, _owner, _sql in schema:
                if kind == "table":
                    cursor.execute(f'SELECT * FROM "{table}" ORDER BY 1')
                    rows[table] = cursor.fetchall()
            return schema, rows

    def test_populated_upgrade_and_reverse_preserve_data_permissions_and_constraints(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        domain_labels = {"core", "identity", "catalogue", "stock", "sales", "customers", "purchasing", "cash"}
        # Freeze the independent backup schema at its last pre-domain dependency.
        # backups.0017 requires identity.0001 and would silently pull the split
        # into the supposedly core.0080 historical fixture's project state.
        # The sync app (US-48) is created after the domain split and depends on identity: leave it out too.
        unchanged_targets = [node for node in latest if node[0] not in domain_labels | {"backups", "sync"}]
        unchanged_targets.append(("backups", "0016_imported_archive_quarantine_updated"))
        try:
            executor.migrate(PREVIOUS)
            old_state = executor.loader.project_state(PREVIOUS + unchanged_targets)
            old = old_state.apps
            user = old.get_model("core", "User").objects.create(username="domain-migration")
            shop = old.get_model("core", "Shop").objects.create(owner=user, name="Boutique historique")
            old.get_model("core", "Membership").objects.create(shop=shop, user=user, role="manager", can_read_cost=True)
            device = old.get_model("core", "Installation").objects.create(user=user)
            product = old.get_model("core", "Product").objects.create(shop=shop, name="Riz", unit_kind="weight",
                unit_label="kg", sale_price_fcfa=1500, stock_threshold_milli=2000)
            pack = old.get_model("core", "ProductPack").objects.create(product=product, name="Sac", base_quantity_milli=25000, sale_price_fcfa=35000)
            old.get_model("core", "Barcode").objects.create(shop=shop, product=product, pack=pack, code="HIST-001")
            event = old.get_model("core", "Activity").objects.create(shop=shop, actor=user, action="stock_opened",
                target_id=product.id, reason="Reprise historique", details={"response": {"quantity": "1.500"}})
            old.get_model("core", "StockMovement").objects.create(product=product, actor=user, operation=event,
                kind="opening", quantity_milli=1500, unit_cost_micros=600123456, occurred_at=timezone.now())
            old.get_model("core", "StockReservation").objects.create(product=product, operation=event, quantity_milli=500, state="pending")
            old.get_model("core", "StockAlertState").objects.create(product=product, is_low=True, episode=1)
            old.get_model("core", "StockAlertEvent").objects.create(product=product, operation=event, episode=1,
                available_milli=1000, threshold_milli=2000)
            content_type = old.get_model("contenttypes", "ContentType").objects.get(app_label="core", model="product")
            permission = old.get_model("auth", "Permission").objects.get(content_type=content_type, codename="view_product")
            group = old.get_model("auth", "Group").objects.create(name="Historique catalogue")
            group.permissions.add(permission)
            user.groups.add(group)
            log = old.get_model("admin", "LogEntry").objects.create(user=user, content_type=content_type,
                object_id=str(product.id), object_repr="Riz", action_flag=2, change_message="Modification historique")
            before = self.snapshot()
            # This database contains one product. Capture its pre-split valuation
            # fingerprint using the historical registry and its original labels.
            cost_sources = ("Product", "StockMovement", "StockLoss", "StockCount", "StockCountAdjustment",
                "StockValuationAdjustment", "StockShortageReview", "CostRevaluation", "SaleLine", "SaleReturnLine",
                "StockCostSnapshot", "PurchaseLine", "PurchaseReceiptLine", "SupplierReturnLine", "PurchaseChargeAllocation",
                "PurchaseChargeRevision", "ReturnReceipt", "ReturnCostSnapshot", "ReturnCostRevision",
                "ReturnCostReconstruction", "NonSellableDecision")
            historical_digest = hashlib.sha256()
            for name in cost_sources:
                historical_model = old.get_model("core", name)
                historical_digest.update(historical_model._meta.label.encode())
                for row in historical_model.objects.order_by("id").values():
                    historical_digest.update(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode())
                    historical_digest.update(b"\n")
            before_fields = {key: {name: field.deconstruct() for name, field in model.fields.items()} for key, model in old_state.models.items()}
            # Exercise the operation against a clone sharing predecessor fields.
            from importlib import import_module
            relocation = import_module("core.migrations.0081_domain_apps").Migration.operations[0]
            next_state = old_state.clone()
            relocation.state_forwards("core", next_state)
            self.assertEqual(next_state.apps.get_model("catalogue", "Product")._meta.get_field("shop").related_model._meta.app_label, "identity")
            self.assertEqual({key: {name: field.deconstruct() for name, field in model.fields.items()} for key, model in old_state.models.items()}, before_fields)
            executor = MigrationExecutor(connection)
            # Freeze this check at the app split: later business migrations may
            # legitimately add constraints or fields to the physical schema.
            executor.migrate(DOMAIN_BRIDGE + unchanged_targets)
            current = executor.loader.project_state(DOMAIN_BRIDGE + unchanged_targets).apps
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(settings.AUTH_USER_MODEL, "core.User")
            for label, model in (("identity", "Shop"), ("catalogue", "Product"), ("stock", "StockMovement"),
                    ("sales", "Sale"), ("customers", "Customer"), ("purchasing", "Supplier"), ("cash", "CashMovement")):
                with self.subTest(domain=label):
                    self.assertEqual(current.get_model(label, model)._meta.db_table, f"core_{model.lower()}")
                    self.assertEqual(apps.get_model(label, model)._meta.app_label, label)
            NewProduct = current.get_model("catalogue", "Product")
            self.assertEqual(NewProduct.objects.get(id=product.id).shop.owner_id, user.id)
            # Schema equality above qualifies the historical app split. Runtime
            # queries below require subsequent business tables as well, including
            # reservation reconciliations. Exercise them on the current schema;
            # keep all historical identity, value and reverse checks intact.
            MigrationExecutor(connection).migrate(latest)
            from stock.services.cost_revaluation_preview import cost_input_fingerprint
            from stock.services.stock_queries import with_stock
            RuntimeProduct = apps.get_model("catalogue", "Product")
            migrated_product = RuntimeProduct.objects.get(id=product.id)
            self.assertEqual(cost_input_fingerprint(migrated_product), historical_digest.hexdigest())
            available = with_stock(RuntimeProduct.objects.filter(id=product.id)).get()
            self.assertEqual(int(available.stock_available_milli), 1000)
            self.assertTrue(available.stock_low)
            self.assertEqual(current.get_model("identity", "Installation").objects.get(id=device.id).user_id, user.id)
            self.assertEqual(current.get_model("core", "Activity").objects.get(id=event.id).details,
                {"response": {"quantity": "1.500"}})
            self.assertEqual(current.get_model("contenttypes", "ContentType").objects.get(id=content_type.id).app_label, "catalogue")
            self.assertEqual(current.get_model("auth", "Permission").objects.get(id=permission.id).content_type_id, content_type.id)
            self.assertEqual(list(current.get_model("auth", "Group").objects.get(id=group.id).permissions.values_list("id", flat=True)), [permission.id])
            self.assertEqual(current.get_model("admin", "LogEntry").objects.get(id=log.id).content_type_id, content_type.id)
            self.assertEqual(list(current.get_model("core", "User").objects.get(id=user.id).groups.values_list("id", flat=True)), [group.id])
            with self.assertRaises(IntegrityError), transaction.atomic():
                NewProduct.objects.filter(id=product.id).update(stock_threshold_milli=-1)
            with self.assertRaises(IntegrityError), transaction.atomic():
                current.get_model("catalogue", "Barcode").objects.create(shop_id=shop.id, product_id=product.id, code="HIST-001")
            self.assertEqual({key: {name: field.deconstruct() for name, field in model.fields.items()} for key, model in old_state.models.items()}, before_fields)
            executor = MigrationExecutor(connection)
            executor.migrate(PREVIOUS)
            self.assertEqual(self.snapshot(), before)
            restored = executor.loader.project_state(PREVIOUS).apps
            self.assertEqual(restored.get_model("contenttypes", "ContentType").objects.get(id=content_type.id).app_label, "core")
            self.assertEqual(restored.get_model("core", "Product").objects.get(id=product.id).name, "Riz")
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_content_type_collision_stops_upgrade_without_partial_relabeling(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate(PREVIOUS)
            old = executor.loader.project_state(PREVIOUS).apps
            ContentType = old.get_model("contenttypes", "ContentType")
            original = list(ContentType.objects.order_by("id").values_list("id", "app_label", "model"))
            collision = ContentType.objects.create(app_label="catalogue", model="product")
            try:
                with self.assertRaisesMessage(RuntimeError, "Conflicting content types"):
                    MigrationExecutor(connection).migrate(latest)
                self.assertEqual(list(ContentType.objects.exclude(id=collision.id).order_by("id").values_list("id", "app_label", "model")), original)
            finally:
                collision.delete()
        finally:
            MigrationExecutor(connection).migrate(latest)
