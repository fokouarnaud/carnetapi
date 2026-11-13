"""New storage constraints preserve history and refuse lossy legacy upgrades."""
import uuid

from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class PrdNumericMigrationTests(TransactionTestCase):
    def snapshot(self, table, columns=None):
        # Later migrations may add columns; preservation is checked on the columns that existed before.
        quote = connection.ops.quote_name
        with connection.cursor() as cursor:
            cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=%s", [table])
            definition = cursor.fetchone()[0]
            selected = ", ".join(quote(column) for column in columns) if columns else "*"
            cursor.execute(f"SELECT {selected} FROM {quote(table)} ORDER BY id")
            return definition, cursor.fetchall()

    def columns(self, table):
        with connection.cursor() as cursor:
            return [row.name for row in connection.introspection.get_table_description(cursor, table)]

    def seed(self, registry, label):
        owner = registry.get_model("core", "User").objects.create(username=str(uuid.uuid4()))
        shop = registry.get_model("identity", "Shop").objects.create(owner=owner, name="Historique")
        installation = registry.get_model("identity", "Installation").objects.create(user=owner)
        event = registry.get_model("core", "Activity").objects.create(
            shop=shop, actor=owner, action="numeric_migration", target_id=uuid.uuid4(), reason="Historique",
        )
        now = timezone.now()
        if label == "identity":
            return shop, {"seller_discount_cap_basis_points": 0.5}, {"seller_discount_cap_basis_points": 0}
        if label == "catalogue":
            row = registry.get_model(label, "Product").objects.create(
                shop=shop, name="Coût inconnu", unit_kind="weight", unit_label="kg",
                sale_price_fcfa=9223372036854775807, purchase_price_fcfa=None,
            )
            return row, {"sale_price_fcfa": 1000.5}, {"sale_price_fcfa": 9223372036854775807}
        if label == "sales":
            row = registry.get_model(label, "Sale").objects.create(
                shop=shop, actor=owner, installation=installation, operation=event, occurred_at=now,
                reference_total_fcfa=1000, discount_total_fcfa=0, net_total_fcfa=1000,
            )
            return row, {"reference_total_fcfa": 1000.5, "net_total_fcfa": 1000.5}, {"reference_total_fcfa": 1000, "net_total_fcfa": 1000}
        if label == "cash":
            row = registry.get_model(label, "CashMovement").objects.create(
                shop=shop, actor=owner, installation=installation, operation=event, occurred_at=now,
                method="cash", manual_kind="withdrawal", amount_fcfa=-1000, reason="Retrait historique",
            )
            return row, {"amount_fcfa": -1000.5}, {"amount_fcfa": -1000}
        if label == "customers":
            customer = registry.get_model(label, "Customer").objects.create(shop=shop, name="Client")
            row = registry.get_model(label, "CustomerCredit").objects.create(
                shop=shop, customer=customer, operation=event, occurred_at=now, amount_fcfa=1000, remaining_fcfa=1000,
            )
        else:
            supplier = registry.get_model(label, "Supplier").objects.create(shop=shop, name="Fournisseur")
            row = registry.get_model(label, "SupplierDebt").objects.create(
                shop=shop, supplier=supplier, operation=event, installation=installation,
                occurred_at=now, amount_fcfa=1000, remaining_fcfa=1000,
            )
        return row, {"amount_fcfa": 1000.5, "remaining_fcfa": 1000.5}, {"amount_fcfa": 1000, "remaining_fcfa": 1000}

    def update_raw(self, row, values):
        quote = connection.ops.quote_name
        assignments = ", ".join(f"{quote(field)}=%s" for field in values)
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {quote(row._meta.db_table)} SET {assignments} WHERE id=%s",
                           [*values.values(), row.pk.hex])

    def test_each_domain_preserves_rows_and_rejects_fractional_history_without_rounding(self):
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        predecessors = {
            "identity": "0001_domain_state", "catalogue": "0001_domain_state",
            "sales": "0001_domain_state", "cash": "0001_domain_state",
            "customers": "0003_integral_collection_values", "purchasing": "0001_domain_state",
        }
        for label, predecessor in predecessors.items():
            with self.subTest(domain=label):
                previous = [node for node in latest if node[0] != label] + [(label, predecessor)]
                row = None
                try:
                    MigrationExecutor(connection).migrate(previous)
                    old = MigrationExecutor(connection).loader.project_state(previous).apps
                    row, invalid, original = self.seed(old, label)
                    before_columns = self.columns(row._meta.db_table)
                    valid_before = self.snapshot(row._meta.db_table, before_columns)[1]
                    self.update_raw(row, invalid)
                    invalid_before = self.snapshot(row._meta.db_table)
                    with self.assertRaises(IntegrityError):
                        MigrationExecutor(connection).migrate(latest)
                    self.assertEqual(self.snapshot(row._meta.db_table), invalid_before)
                    self.update_raw(row, original)
                    MigrationExecutor(connection).migrate(latest)
                    self.assertEqual(self.snapshot(row._meta.db_table, before_columns)[1], valid_before)
                    with connection.cursor() as cursor:
                        cursor.execute("PRAGMA foreign_key_check")
                        self.assertEqual(cursor.fetchall(), [])
                finally:
                    if row is not None:
                        self.update_raw(row, original)
                    MigrationExecutor(connection).migrate(latest)
