"""US-14: explicit non-sellable outcomes retain origin and distinct stock effects."""
import uuid
from django.db import connection, IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from stock.tests import test_return_receipts as receipt_tests
from common.models import Activity
from cash.models import CashMovement
from stock.models import NonSellableDecision, ReturnReceipt, StockLoss, StockMovement


class NonSellableDecisionModelTests(TestCase):
    setUp = receipt_tests.ReturnReceiptTests.setUp
    authenticate = receipt_tests.ReturnReceiptTests.authenticate
    payload = receipt_tests.ReturnReceiptTests.payload
    url = receipt_tests.ReturnReceiptTests.url
    prepare_sale = receipt_tests.ReturnReceiptTests.prepare_sale
    return_url = receipt_tests.ReturnReceiptTests.return_url
    return_payload = receipt_tests.ReturnReceiptTests.return_payload
    prepare_return = receipt_tests.ReturnReceiptTests.prepare_return
    receipts_url = receipt_tests.ReturnReceiptTests.receipts_url
    receipt_payload = receipt_tests.ReturnReceiptTests.receipt_payload

    def prepare_unsellable(self):
        sale, returned = self.prepare_return()
        result = self.client.post(self.receipts_url(sale, returned), self.receipt_payload(quantity=2000), format="json")
        self.assertEqual(result.status_code, 201)
        return ReturnReceipt.objects.get(id=result.data["id"])

    def test_scrap_retains_origin_without_second_available_stock_or_money_effect(self):
        receipt = self.prepare_unsellable()
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="unsellable_decided", target_id=uuid.uuid4(), reason="Produit détruit")
        decision = NonSellableDecision.objects.create(id=event.target_id, receipt=receipt, operation=event,
            installation=receipt.installation, kind="scrap", quantity_milli=1000, after_quantity_milli=1000,
            unit_kind="piece", occurred_at=timezone.now())
        self.assertEqual(decision.receipt.return_line.sale_line.product_id, self.product.id)
        self.assertIsNone(decision.movement_id)
        self.assertEqual(StockMovement.objects.count(), 2)
        self.assertEqual(CashMovement.objects.count(), 2)
        self.assertFalse(StockLoss.objects.exists())
        for changes in ({"quantity_milli": 0}, {"quantity_milli": 1.5}, {"quantity_milli": 1500},
                        {"after_quantity_milli": -1}, {"after_quantity_milli": 1.5}, {"unit_kind": "unknown"},
                        {"kind": "restock"}, {"kind": "supplier_return"}, {"is_sellable_confirmed": True}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                field, value = next(iter(changes.items()))
                with connection.cursor() as cursor:
                    cursor.execute(f'UPDATE core_nonsellabledecision SET "{field}" = %s WHERE id = %s', [value, decision.id.hex])
