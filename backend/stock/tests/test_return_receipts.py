"""US-14/26: receipt and sellable state alone control physical stock additions."""
import uuid
from django.test import TestCase
from django.utils import timezone
from sales.tests import test_sale_returns as return_tests
from cash.models import CashMovement
from catalogue.models import Product
from stock.models import ReturnReceipt, StockAlertEvent, StockAlertState, StockMovement
from sales.models import SaleLine


class ReturnReceiptTests(TestCase):
    setUp = return_tests.SaleReturnTests.setUp
    authenticate = return_tests.SaleReturnTests.authenticate
    payload = return_tests.SaleReturnTests.payload
    url = return_tests.SaleReturnTests.url
    prepare_sale = return_tests.SaleReturnTests.prepare_sale
    return_url = return_tests.SaleReturnTests.return_url
    return_payload = return_tests.SaleReturnTests.return_payload

    def prepare_return(self):
        sale = self.prepare_sale()
        result = self.client.post(self.return_url(sale), self.return_payload(sale, 2000), format="json")
        self.assertEqual(result.status_code, 201)
        return sale, result.data

    def receipts_url(self, sale, returned):
        return self.return_url(sale) + returned["id"] + "/lines/" + returned["lines"][0]["id"] + "/receipts/"

    def receipt_payload(self, condition="unsellable", quantity=1000):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "quantity_milli": quantity,
                "condition": condition, "occurred_at": timezone.now().isoformat(), "reason": "État physique vérifié"}

    def test_unsellable_receipt_is_durable_without_restock_or_refund_and_sellable_part_is_added_once(self):
        StockMovement.objects.filter(kind="opening").update(unit_cost_micros=None)
        sale, returned = self.prepare_return()
        path = self.receipts_url(sale, returned)
        data = self.receipt_payload()
        received = self.client.post(path, data, format="json")
        self.assertEqual(received.status_code, 201, received.content)
        self.assertIsNone(received.data["movement_id"])
        self.assertIsNone(received.data["unit_cost_micros"])
        self.assertTrue(received.data["cost_is_provisional"])
        self.assertEqual(self.client.post(path, data, format="json").data, received.data)
        self.assertEqual(StockMovement.objects.count(), 2)
        sellable = self.receipt_payload("sellable")
        added = self.client.post(path, sellable, format="json")
        self.assertEqual(added.status_code, 201, added.content)
        self.assertIsNotNone(added.data["movement_id"])
        self.assertEqual(self.client.post(path, sellable, format="json").status_code, 200)
        self.assertEqual(StockMovement.objects.get(kind="customer_return").quantity_milli, 1000)
        self.assertEqual(ReturnReceipt.objects.count(), 2)
        self.assertEqual(self.client.post(path, self.receipt_payload(), format="json").status_code, 409)
        self.assertEqual(CashMovement.objects.count(), 2)
        self.assertEqual(self.client.get(path).data["count"], 2)

    def test_cost_comes_from_original_sale_line_and_archived_product_can_be_received(self):
        StockMovement.objects.filter(kind="opening").update(unit_cost_micros=1200000000)
        sale, returned = self.prepare_return()
        Product.objects.filter(id=self.product.id).update(is_active=False, purchase_price_fcfa=9999)
        result = self.client.post(self.receipts_url(sale, returned), self.receipt_payload("sellable"), format="json")
        self.assertEqual(result.status_code, 201, result.content)
        self.assertEqual(result.data["unit_cost_micros"], 1200000000)
        self.assertFalse(result.data["cost_is_provisional"])
        self.assertEqual(StockMovement.objects.get(kind="customer_return").unit_cost_micros, 1200000000)
        SaleLine.objects.update(unit_cost_micros=9900000000)
        saved = self.client.get(self.receipts_url(sale, returned)).data["results"][0]
        self.assertEqual(saved["unit_cost_micros"], 1200000000)

    def test_seller_foreign_origin_piece_fraction_and_changed_replay_are_refused(self):
        sale, returned = self.prepare_return()
        path = self.receipts_url(sale, returned)
        data = self.receipt_payload()
        self.authenticate(self.seller)
        self.assertEqual(self.client.post(path, data, format="json").status_code, 403)
        self.assertEqual(self.client.get(path).status_code, 403)
        self.authenticate(self.owner)
        self.assertEqual(self.client.post(path.replace(str(self.shop.id), str(self.other.id)), data, format="json").status_code, 404)
        self.assertEqual(self.client.post(path, self.receipt_payload(quantity=1500), format="json").status_code, 400)
        self.assertEqual(self.client.post(path, data, format="json").status_code, 201)
        self.assertEqual(self.client.post(path, {**data, "condition": "sellable"}, format="json").status_code, 409)
        self.assertFalse(StockMovement.objects.filter(kind="customer_return").exists())

    def test_sellable_receipt_rearms_low_stock_crossing_but_unsellable_receipt_does_not(self):
        Product.objects.filter(id=self.product.id).update(stock_threshold_milli=8000)
        sale, returned = self.prepare_return()
        path = self.receipts_url(sale, returned)
        self.assertTrue(StockAlertState.objects.get(product=self.product).is_low)
        self.assertEqual(StockAlertEvent.objects.filter(product=self.product).count(), 1)
        self.assertEqual(self.client.post(path, self.receipt_payload(), format="json").status_code, 201)
        self.assertTrue(StockAlertState.objects.get(product=self.product).is_low)
        self.assertEqual(self.client.post(path, self.receipt_payload("sellable"), format="json").status_code, 201)
        self.assertFalse(StockAlertState.objects.get(product=self.product).is_low)
        self.assertEqual(StockAlertEvent.objects.filter(product=self.product).count(), 1)
        next_sale = self.payload(lines=[{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1, "quantity_milli": 1000}],
            payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 5000}])
        self.assertEqual(self.client.post(self.url(), next_sale, format="json").status_code, 201)
        self.assertTrue(StockAlertState.objects.get(product=self.product).is_low)
        self.assertEqual(StockAlertEvent.objects.filter(product=self.product).count(), 2)
