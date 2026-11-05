"""US-14/26: durable return origin and physical condition, without implicit cash."""
import uuid
from django.db import IntegrityError, transaction, connection
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone
from sales.tests import test_sales as sale_tests
from common.models import Activity
from cash.models import CashMovement
from stock.models import ReturnReceipt, StockMovement
from sales.models import Sale, SaleReturn, SaleReturnLine


class SaleReturnModelTests(TestCase):
    setUp = sale_tests.CashSaleTests.setUp
    authenticate = sale_tests.CashSaleTests.authenticate
    payload = sale_tests.CashSaleTests.payload
    url = sale_tests.CashSaleTests.url

    def prepare_return(self):
        self.assertEqual(self.client.post(self.url(), self.payload(), format="json").status_code, 201)
        sale = Sale.objects.get()
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="sale_returned", target_id=uuid.uuid4(), reason="Retour demandé")
        returned = SaleReturn.objects.create(id=event.target_id, sale=sale, operation=event, installation=self.device, occurred_at=timezone.now())
        line = SaleReturnLine.objects.create(sale_return=returned, sale_line=sale.lines.get(), quantity_milli=1000, amount_fcfa=5000, original_quantity_milli=2000, original_amount_fcfa=10000, unit_kind="piece")
        return returned, line

    def test_origin_survives_without_restock_refund_or_mutation_of_sale(self):
        returned, line = self.prepare_return()
        self.assertEqual(line.sale_line.sale_id, returned.sale_id)
        self.assertEqual(line.amount_fcfa, 5000)
        self.assertEqual(Sale.objects.get().net_total_fcfa, 10000)
        self.assertEqual(StockMovement.objects.filter(kind="sale").count(), 1)
        self.assertEqual(CashMovement.objects.count(), 2)
        self.assertEqual(StockMovement.objects.count(), 2)
        with self.assertRaises(ProtectedError):
            line.sale_line.delete()
        with self.assertRaises(ProtectedError):
            returned.operation.delete()

    def test_return_line_quantities_and_amounts_are_exact_and_bounded_by_snapshots(self):
        _, line = self.prepare_return()
        for changes in ({"quantity_milli": 0}, {"quantity_milli": 1.5}, {"quantity_milli": 1500}, {"quantity_milli": 3000}, {"amount_fcfa": 11000},
                        {"amount_fcfa": -1}, {"amount_fcfa": 1.5}, {"unit_kind": "unknown"}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    field, value = next(iter(changes.items()))
                    cursor.execute(f'UPDATE core_salereturnline SET "{field}" = %s WHERE id = %s', [value, line.id.hex])

    def test_physical_return_preserves_origin_and_unknown_cost_without_available_stock(self):
        _, line = self.prepare_return()
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="return_received", target_id=uuid.uuid4(), reason="Article abîmé reçu")
        receipt = ReturnReceipt.objects.create(id=event.target_id, return_line=line, operation=event,
            installation=self.device, quantity_milli=1000, condition="unsellable", occurred_at=timezone.now(),
            unit_cost_micros=None, cost_is_provisional=True)
        self.assertIsNone(receipt.unit_cost_micros)
        self.assertTrue(receipt.cost_is_provisional)
        self.assertEqual(receipt.return_line.sale_line.product_id, self.product.id)
        self.assertEqual(StockMovement.objects.count(), 2)
        self.assertEqual(CashMovement.objects.count(), 2)
        for changes in ({"quantity_milli": 0}, {"quantity_milli": 1.5}, {"condition": "unknown"},
                        {"unit_cost_micros": -1}, {"unit_cost_micros": 1.5}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    field, value = next(iter(changes.items()))
                    cursor.execute(f'UPDATE core_returnreceipt SET "{field}" = %s WHERE id = %s', [value, receipt.id.hex])
