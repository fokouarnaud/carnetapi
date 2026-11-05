"""Run explicitly: upgrade populated 0077 value confirmations without changing them."""
from django.db import connection, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class StockValuationMigrationChecks(TransactionTestCase):
    def test_upgrade_preserves_positive_decisions_and_adds_strict_empty_closure(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        previous = [("core", "0077_stockvaluationadjustment")]
        try:
            executor.migrate(previous)
            old = executor.loader.project_state(previous).apps
            user = old.get_model("core", "User").objects.create(username="valuation-migration")
            shop = old.get_model("core", "Shop").objects.create(owner=user, name="Boutique")
            product = old.get_model("core", "Product").objects.create(shop=shop, name="Produit", unit_kind="piece",
                unit_label="pièce", sale_price_fcfa=100)
            device = old.get_model("core", "Installation").objects.create(user=user)
            Activity = old.get_model("core", "Activity")
            Valuation = old.get_model("core", "StockValuationAdjustment")
            for quantity, before, confirmed in ((500, None, 600), (1000, 1000000000000, 900)):
                event = Activity.objects.create(shop=shop, actor=user, action="stock_valuation_confirmed", target_id=product.id,
                    reason="Valeur contrôlée", details={"response": {"value_fcfa": str(confirmed)}})
                Valuation.objects.create(product=product, operation=event, installation=device, occurred_at=timezone.now(),
                    stock_quantity_milli=quantity, previous_value_nanos_fcfa=before, value_fcfa=confirmed, basis_fingerprint="a" * 64)
            saved = list(Valuation.objects.order_by("id").values())
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            current = executor.loader.project_state(latest).apps
            Updated = current.get_model("stock", "StockValuationAdjustment")
            self.assertEqual(list(Updated.objects.order_by("id").values()), saved)
            event = current.get_model("core", "Activity").objects.create(shop_id=shop.id, actor_id=user.id,
                action="stock_valuation_confirmed", target_id=product.id, reason="Écart sur stock nul examiné")
            closure = Updated.objects.create(product_id=product.id, operation=event, installation_id=device.id,
                occurred_at=timezone.now(), stock_quantity_milli=0, previous_value_nanos_fcfa=0, value_fcfa=0, basis_fingerprint="b" * 64)
            self.assertEqual(Updated.objects.get(id=closure.id).value_fcfa, 0)
            with self.assertRaises(IntegrityError), transaction.atomic():
                Updated.objects.filter(id=closure.id).update(value_fcfa=1)
            self.assertEqual(current.get_model("stock", "StockMovement").objects.count(), 0)
            self.assertEqual(current.get_model("cash", "CashMovement").objects.count(), 0)
        finally:
            MigrationExecutor(connection).migrate(latest)
