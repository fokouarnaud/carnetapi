"""US-26: cancelling retains the original sale and its separate compensating return."""
import uuid
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone
from sales.tests import test_sale_returns as return_tests
from common.models import Activity
from cash.models import CashMovement
from sales.models import Sale, SaleCancellation, SalePayment
from stock.models import StockMovement


class SaleCancellationModelTests(TestCase):
    setUp = return_tests.SaleReturnTests.setUp
    authenticate = return_tests.SaleReturnTests.authenticate
    payload = return_tests.SaleReturnTests.payload
    url = return_tests.SaleReturnTests.url
    prepare_sale = return_tests.SaleReturnTests.prepare_sale
    return_url = return_tests.SaleReturnTests.return_url
    return_payload = return_tests.SaleReturnTests.return_payload

    def test_cancellation_preserves_original_sale_payments_and_optional_return_origin(self):
        sale_data = self.prepare_sale()
        returned = self.client.post(self.return_url(sale_data), self.return_payload(sale_data, 2000), format="json")
        self.assertEqual(returned.status_code, 201)
        sale = Sale.objects.get()
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="sale_cancelled", target_id=sale.id, reason="Vente saisie par erreur")
        cancellation = SaleCancellation.objects.create(sale=sale, operation=event, installation=sale.installation,
            occurred_at=timezone.now(), sale_return_id=returned.data["id"])
        self.assertEqual(cancellation.sale.net_total_fcfa, 10000)
        self.assertEqual(cancellation.sale_return.amount_fcfa, 10000)
        self.assertEqual(SalePayment.objects.count(), 2)
        self.assertEqual(CashMovement.objects.count(), 2)
        self.assertEqual(StockMovement.objects.count(), 2)
        with self.assertRaises(ProtectedError):
            cancellation.sale_return.delete()
        with self.assertRaises(ProtectedError):
            sale.delete()
        other_event = Activity.objects.create(shop=self.shop, actor=self.owner, action="sale_cancelled", target_id=sale.id, reason="Autre essai")
        with self.assertRaises(IntegrityError), transaction.atomic():
            SaleCancellation.objects.create(sale=sale, operation=other_event, installation=sale.installation, occurred_at=timezone.now())
