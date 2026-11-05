"""US-30: confirm physical departures independently of credit and cash."""
import uuid
from django.test import TestCase
from cash.models import CashMovement
from stock.models import StockMovement
from purchasing.models import SupplierDebt
from purchasing.tests import test_purchases


class SupplierReturnTests(TestCase):
    setUp = test_purchases.PurchaseTests.setUp
    authenticate = test_purchases.PurchaseTests.authenticate
    url = test_purchases.PurchaseTests.url
    invoice_payload = test_purchases.PurchaseTests.payload
    receipt_payload = test_purchases.PurchaseTests.receipt_payload

    def invoice(self, receive=True):
        invoice = self.client.post(self.url(), self.invoice_payload(), format="json")
        self.assertEqual(invoice.status_code, 201)
        if receive:
            receipt = self.client.post(self.url() + invoice.data["id"] + "/receipts/", self.receipt_payload(invoice.data), format="json")
            self.assertEqual(receipt.status_code, 201)
        return invoice.data

    def return_payload(self, invoice, quantity=3000):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-03T00:00:00Z",
                "reason": "Trois cartons retournés physiquement", "lines": [{"line_id": invoice["lines"][0]["id"], "quantity_milli": quantity}]}

    def test_physical_return_and_retry_reduce_only_received_stock_without_credit_or_cash(self):
        invoice = self.invoice()
        url = self.url() + invoice["id"] + "/returns/"
        data = self.return_payload(invoice)
        result = self.client.post(url, data, format="json")
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.data["lines"][0]["base_quantity_milli"], 72000)
        retry = self.client.post(url, data, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, result.data)
        self.assertEqual(sum(StockMovement.objects.values_list("quantity_milli", flat=True)), 72000)
        self.assertEqual(StockMovement.objects.filter(kind="supplier_return").count(), 1)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 10000)
        self.assertFalse(CashMovement.objects.exists())
        self.assertEqual(self.client.get(url).data["count"], 1)

    def test_cumulative_return_cannot_exceed_received_quantity_less_previous_returns(self):
        invoice = self.invoice()
        url = self.url() + invoice["id"] + "/returns/"
        self.assertEqual(self.client.post(url, self.return_payload(invoice, 4000), format="json").status_code, 201)
        self.assertEqual(self.client.post(url, self.return_payload(invoice, 3000), format="json").status_code, 400)
        self.assertEqual(sum(StockMovement.objects.values_list("quantity_milli", flat=True)), 48000)
        self.assertEqual(self.client.post(url, self.return_payload(invoice, 2000), format="json").status_code, 201)
        self.assertEqual(sum(StockMovement.objects.values_list("quantity_milli", flat=True)), 0)

    def test_unreceived_goods_and_seller_departures_are_refused(self):
        invoice = self.invoice(receive=False)
        url = self.url() + invoice["id"] + "/returns/"
        self.assertEqual(self.client.post(url, self.return_payload(invoice), format="json").status_code, 400)
        self.authenticate(self.seller)
        self.assertEqual(self.client.post(url, self.return_payload(invoice), format="json").status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertFalse(StockMovement.objects.exists())

    def test_foreign_line_in_second_return_line_rolls_back_departure(self):
        invoice = self.invoice()
        foreign = self.invoice()
        data = self.return_payload(invoice, 1000)
        data["lines"].append({"line_id": foreign["lines"][0]["id"], "quantity_milli": 1000})
        url = self.url() + invoice["id"] + "/returns/"
        self.assertEqual(self.client.post(url, data, format="json").status_code, 404)
        self.assertFalse(StockMovement.objects.filter(kind="supplier_return").exists())
        self.assertEqual(sum(StockMovement.objects.values_list("quantity_milli", flat=True)), 288000)

    def test_changed_return_retry_does_not_add_another_departure(self):
        invoice = self.invoice()
        url = self.url() + invoice["id"] + "/returns/"
        data = self.return_payload(invoice)
        self.assertEqual(self.client.post(url, data, format="json").status_code, 201)
        data["lines"][0]["quantity_milli"] = 1000
        self.assertEqual(self.client.post(url, data, format="json").status_code, 409)
        self.assertEqual(StockMovement.objects.get(kind="supplier_return").quantity_milli, -72000)
