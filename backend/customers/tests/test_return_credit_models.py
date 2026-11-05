"""US-23/26: return credit decisions retain distinct origin and cash source."""
import uuid
from django.db import connection, IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from sales.tests import test_sale_returns as return_tests
from common.models import Activity
from cash.models import CashMovement
from customers.models import ReturnCreditSettlement
from sales.models import Sale, SalePayment, SaleReturn


class ReturnCreditModelTests(TestCase):
    setUp = return_tests.SaleReturnTests.setUp
    authenticate = return_tests.SaleReturnTests.authenticate
    payload = return_tests.SaleReturnTests.payload
    url = return_tests.SaleReturnTests.url
    prepare_sale = return_tests.SaleReturnTests.prepare_sale
    return_url = return_tests.SaleReturnTests.return_url
    return_payload = return_tests.SaleReturnTests.return_payload

    def prepare_credit(self):
        sale = self.prepare_sale()
        response = self.client.post(self.return_url(sale), self.return_payload(sale, 2000), format="json")
        self.assertEqual(response.status_code, 201)
        return SaleReturn.objects.get(id=response.data["id"])

    def test_refund_decision_is_distinct_and_can_source_one_negative_cash_movement(self):
        returned = self.prepare_credit()
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="return_credit_refund", target_id=uuid.uuid4(), reason="Argent rendu")
        decision = ReturnCreditSettlement.objects.create(id=event.target_id, sale_return=returned, operation=event,
            installation=returned.installation, amount_fcfa=3000, after_credit_fcfa=7000, kind="refund", method="cash", occurred_at=timezone.now())
        self.assertEqual(CashMovement.objects.count(), 2)
        movement = CashMovement.objects.create(shop=self.shop, actor=self.owner, installation=returned.installation,
            operation=event, sale_return_refund=decision, method="cash", amount_fcfa=-3000, occurred_at=decision.occurred_at, reason="Argent rendu")
        self.assertEqual(movement.sale_return_refund.sale_return_id, returned.id)
        for changes in ({"amount_fcfa": 3000}, {"manual_kind": "expense"}, {"sale_payment_id": SalePayment.objects.first().id.hex}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                field, value = next(iter(changes.items()))
                with connection.cursor() as cursor:
                    cursor.execute(f'UPDATE core_cashmovement SET "{field}" = %s WHERE id = %s', [value, movement.id.hex])
        self.assertEqual(Sale.objects.get().net_total_fcfa, 10000)

    def test_database_rejects_fractional_money_and_inconsistent_decision_kind(self):
        returned = self.prepare_credit()
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="return_credit_refund", target_id=uuid.uuid4(), reason="Argent rendu")
        decision = ReturnCreditSettlement.objects.create(sale_return=returned, operation=event, installation=returned.installation,
            amount_fcfa=3000, after_credit_fcfa=7000, kind="refund", method="cash", occurred_at=timezone.now())
        for changes in ({"amount_fcfa": 0}, {"amount_fcfa": 1.5}, {"after_credit_fcfa": -1}, {"after_credit_fcfa": 1.5},
                        {"kind": "unknown"}, {"kind": "allocation"}, {"method": ""}, {"method": "other"}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                field, value = next(iter(changes.items()))
                with connection.cursor() as cursor:
                    cursor.execute(f'UPDATE core_returncreditsettlement SET "{field}" = %s WHERE id = %s', [value, decision.id.hex])
