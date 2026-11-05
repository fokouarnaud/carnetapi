"""Existing collections must keep their original unallocated effect on upgrade."""
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class CustomerLedgerMigrationTests(TransactionTestCase):
    def test_existing_opening_collection_and_later_allocation_are_recovered(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        previous = [("core", "0017_customerdebt_due_version_and_more")]
        try:
            executor.migrate(previous)
            old = executor.loader.project_state(previous).apps
            User = old.get_model("core", "User")
            Shop = old.get_model("core", "Shop")
            Customer = old.get_model("core", "Customer")
            Installation = old.get_model("core", "Installation")
            Activity = old.get_model("core", "Activity")
            Debt = old.get_model("core", "CustomerDebt")
            Payment = old.get_model("core", "CustomerPayment")
            Allocation = old.get_model("core", "DebtAllocation")
            Regularization = old.get_model("core", "PaymentRegularization")
            RegularizationAllocation = old.get_model("core", "RegularizationAllocation")
            user = User.objects.create(username="legacy-ledger")
            shop = Shop.objects.create(name="Legacy", owner=user)
            customer = Customer.objects.create(shop=shop, name="Client")
            device = Installation.objects.create(user=user)
            at = timezone.now()
            opening_event = Activity.objects.create(shop=shop, actor=user, action="customer_debt_opened", target_id=customer.id, reason="Ouverture")
            debt = Debt.objects.create(shop=shop, customer=customer, actor=user, installation=device, operation=opening_event,
                                       amount_fcfa=10000, remaining_fcfa=5000, occurred_at=at)
            payment_event = Activity.objects.create(shop=shop, actor=user, action="customer_payment_created", target_id=customer.id, reason="Encaissement")
            payment = Payment.objects.create(shop=shop, customer=customer, actor=user, installation=device, operation=payment_event,
                                             amount_fcfa=5000, unallocated_fcfa=0, occurred_at=at)
            Allocation.objects.create(payment=payment, debt=debt, position=0, requested_amount_fcfa=3000, applied_amount_fcfa=3000)
            decision_event = Activity.objects.create(shop=shop, actor=user, action="customer_payment_allocation", target_id=payment.id, reason="Affectation")
            decision = Regularization.objects.create(payment=payment, operation=decision_event, installation=device, kind="allocation",
                                                      amount_fcfa=2000, after_unallocated_fcfa=0, occurred_at=at)
            RegularizationAllocation.objects.create(regularization=decision, debt=debt, position=0, amount_fcfa=2000)
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            current = executor.loader.project_state(latest).apps
            Ledger = current.get_model("customers", "CustomerLedgerEntry")
            entries = list(Ledger.objects.filter(customer_id=customer.id).order_by("recorded_at", "sequence"))
            self.assertEqual(len(entries), 3)
            self.assertEqual([row.delta_fcfa for row in entries], [10000, -5000, 0])
            self.assertEqual([row.credit_delta_fcfa for row in entries], [0, 2000, -2000])
            self.assertEqual(sum(row.debt_delta_fcfa for row in entries), 5000)
            self.assertEqual(sum(row.delta_fcfa for row in entries), 5000)
            self.assertEqual(entries[0].id, uuid.uuid5(debt.id, "customer-ledger:opening_debt"))
            self.assertEqual(entries[0].recorded_at, opening_event.created_at)
        finally:
            MigrationExecutor(connection).migrate(latest)
