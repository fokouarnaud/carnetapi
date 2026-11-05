"""Adding integer storage checks preserves valid history and refuses bad rows."""
from django.db import connection, IntegrityError
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class MovementStorageMigrationTests(TransactionTestCase):
    def previous(self, latest):
        return [node for node in latest if node[0] != "stock"] + [("stock", "0001_domain_state")]

    def seed(self, registry, quantities_and_costs):
        owner = registry.get_model("core", "User").objects.create(username="stock-storage-migration")
        shop = registry.get_model("identity", "Shop").objects.create(owner=owner, name="Stock historique")
        product = registry.get_model("catalogue", "Product").objects.create(shop=shop, name="Riz",
            unit_kind="weight", unit_label="kg", sale_price_fcfa=1500)
        Movement = registry.get_model("stock", "StockMovement")
        rows = []
        for quantity, cost in quantities_and_costs:
            kind = "loss" if quantity < 0 else "opening"
            event = registry.get_model("core", "Activity").objects.create(shop=shop, actor=owner,
                action="stock_loss_recorded" if quantity < 0 else "stock_opened", target_id=product.id, reason="Historique conservé")
            rows.append(Movement.objects.create(product=product, actor=owner, operation=event, kind=kind,
                quantity_milli=quantity, unit_cost_micros=cost, occurred_at=timezone.now(), reason="Ouverture"))
        return rows

    def table_snapshot(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='core_stockmovement'")
            schema = cursor.fetchone()[0]
            cursor.execute("SELECT * FROM core_stockmovement ORDER BY id")
            return schema, cursor.fetchall()

    def test_valid_history_keeps_uuid_dates_quantities_costs_and_links(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        previous = self.previous(latest)
        try:
            executor.migrate(previous)
            old = executor.loader.project_state(previous).apps
            self.seed(old, [(0, None), (-125, 0), (1250, 600123456),
                (9223372036854775807, 9223372036854775807)])
            _schema, before = self.table_snapshot()
            MigrationExecutor(connection).migrate(latest)
            schema, after = self.table_snapshot()
            self.assertEqual(after, before)
            self.assertIn("stock_movement_integer_quantity", schema)
            self.assertIn("stock_movement_integer_known_cost", schema)
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA foreign_key_check")
                self.assertEqual(cursor.fetchall(), [])
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_invalid_legacy_rows_stop_upgrade_without_truncation_or_partial_constraints(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        previous = self.previous(latest)
        try:
            executor.migrate(previous)
            old = executor.loader.project_state(previous).apps
            movement = self.seed(old, [(1000, 600123456)])[0]
            for column, invalid, original, constraint in (
                    ("quantity_milli", 1000.5, 1000, "stock_movement_integer_quantity"),
                    ("unit_cost_micros", 600123456.5, 600123456, "stock_movement_integer_known_cost")):
                with self.subTest(column=column):
                    with connection.cursor() as cursor:
                        cursor.execute(f"UPDATE core_stockmovement SET {column} = %s WHERE id = %s", [invalid, movement.id.hex])
                    before = self.table_snapshot()
                    try:
                        with self.assertRaisesMessage(IntegrityError, constraint):
                            MigrationExecutor(connection).migrate(latest)
                        self.assertEqual(self.table_snapshot(), before)
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT count(*) FROM django_migrations WHERE app='stock' AND name LIKE '0002_%'")
                            self.assertEqual(cursor.fetchone()[0], 0)
                    finally:
                        # Only repair the deliberately corrupted test fixture.
                        # The production migration never rounds or rewrites it.
                        with connection.cursor() as cursor:
                            cursor.execute(f"UPDATE core_stockmovement SET {column} = %s WHERE id = %s", [original, movement.id.hex])
        finally:
            MigrationExecutor(connection).migrate(latest)
