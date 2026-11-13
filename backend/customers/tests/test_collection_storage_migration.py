"""Stored legacy money is audited before SQLite can coerce values in a rebuild."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from datetime import datetime, timezone

from customers.models import CustomerDebt, CustomerPayment, CustomerPaymentPart


class CollectionStorageMigrationTests(TransactionTestCase):
    def seed(self, apps):
        model = apps.get_model
        owner = model("core", "User").objects.create(username="collection-storage-owner")
        shop = model("identity", "Shop").objects.create(owner=owner, name="A")
        customer = model("customers", "Customer").objects.create(shop=shop, name="Client")
        installation = model("identity", "Installation").objects.create(user=owner)
        instant = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
        activity = model("core", "Activity")
        opening = activity.objects.create(shop=shop, actor=owner, action="opening_debt", target_id=customer.id, reason="Ouverture")
        debt = model("customers", "CustomerDebt").objects.create(shop=shop, customer=customer, actor=owner, installation=installation,
            operation=opening, amount_fcfa=5000, remaining_fcfa=3500, occurred_at=instant)
        event = activity.objects.create(shop=shop, actor=owner, action="customer_payment_created", target_id=customer.id, reason="Encaissement")
        payment = model("customers", "CustomerPayment").objects.create(shop=shop, customer=customer, actor=owner, installation=installation,
            operation=event, amount_fcfa=2000, unallocated_fcfa=500, requires_regularization=True, occurred_at=instant)
        part = model("customers", "CustomerPaymentPart").objects.create(payment=payment, position=0, method="cash", amount_fcfa=2000,
            received_fcfa=2500, returned_fcfa=500)
        model("customers", "DebtAllocation").objects.create(payment=payment, debt=debt, position=0,
            requested_amount_fcfa=2000, applied_amount_fcfa=1500)
        self.payment = CustomerPayment(pk=payment.pk)
        self.debt = CustomerDebt(pk=debt.pk)
        self.part = CustomerPaymentPart(pk=part.pk)

    def test_valid_collections_survive_and_fractional_legacy_money_stops_atomically(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            target = [("customers", "0002_customer_customer_shop_name_match_and_more")]
            executor.migrate(target)
            # Seed with the historical models: later migrations add columns absent at this state.
            self.seed(MigrationExecutor(connection).loader.project_state(target).apps)
            with connection.cursor() as cursor:
                cursor.execute("UPDATE core_customerpayment SET unallocated_fcfa=500.5 WHERE id=%s", [self.payment.id.hex])
            with self.assertRaisesMessage(ValueError, "Non-integral"):
                MigrationExecutor(connection).migrate(latest)
            with connection.cursor() as cursor:
                cursor.execute("SELECT unallocated_fcfa FROM core_customerpayment WHERE id=%s", [self.payment.id.hex])
                self.assertEqual(cursor.fetchone()[0], 500.5)
                cursor.execute("UPDATE core_customerpayment SET unallocated_fcfa=500 WHERE id=%s", [self.payment.id.hex])
            MigrationExecutor(connection).migrate(latest)
            self.payment.refresh_from_db(); self.debt.refresh_from_db(); self.part.refresh_from_db()
            self.assertEqual((self.payment.amount_fcfa, self.payment.unallocated_fcfa, self.debt.remaining_fcfa,
                              self.part.received_fcfa, self.part.returned_fcfa), (2000, 500, 3500, 2500, 500))
        finally:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE core_customerpayment SET unallocated_fcfa=500 WHERE unallocated_fcfa=500.5")
            MigrationExecutor(connection).migrate(latest)
