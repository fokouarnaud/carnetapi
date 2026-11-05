"""Add return settlements without fabricating money or changing existing cash."""
import uuid
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class ReturnCreditMigrationTests(TransactionTestCase):
    def test_legacy_cash_and_unsettled_return_credit_are_preserved(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        before = [("core", "0064_alter_stockmovement_kind")]
        try:
            executor.migrate(before)
            apps = executor.loader.project_state(before).apps
            def model(name):
                return apps.get_model("core", name)
            owner = model("User").objects.create(username="return-credit-before")
            shop = model("Shop").objects.create(name="Boutique", owner=owner)
            device = model("Installation").objects.create(user=owner)
            at = timezone.now()
            operation = model("Activity").objects.create(shop=shop, actor=owner, action="sale_created", target_id=uuid.uuid4(), reason="Vente")
            sale = model("Sale").objects.create(shop=shop, actor=owner, installation=device, operation=operation,
                occurred_at=at, reference_total_fcfa=10000, net_total_fcfa=10000, discount_total_fcfa=0)
            paid = model("SalePayment").objects.create(sale=sale, position=0, method="cash", amount_fcfa=4000)
            movement = model("CashMovement").objects.create(shop=shop, actor=owner, installation=device, operation=operation,
                sale_payment=paid, method="cash", amount_fcfa=4000, occurred_at=at, reason="Paiement historique")
            returned_op = model("Activity").objects.create(shop=shop, actor=owner, action="sale_return_created", target_id=uuid.uuid4(), reason="Retour historique")
            returned = model("SaleReturn").objects.create(sale=sale, operation=returned_op, installation=device,
                occurred_at=at, amount_fcfa=10000, debt_reduced_fcfa=6000, credit_fcfa=4000)
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            current = executor.loader.project_state(latest).apps
            cash = current.get_model("cash", "CashMovement").objects.get(id=movement.id)
            self.assertEqual(cash.amount_fcfa, 4000)
            self.assertEqual(cash.sale_payment_id, paid.id)
            self.assertIsNone(cash.sale_return_refund_id)
            self.assertEqual(current.get_model("sales", "SaleReturn").objects.get(id=returned.id).credit_fcfa, 4000)
            self.assertEqual(current.get_model("customers", "ReturnCreditSettlement").objects.count(), 0)
        finally:
            MigrationExecutor(connection).migrate(latest)
