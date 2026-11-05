"""US-26: real returns reduce the original debt before creating a separate claim."""
import uuid
from django.test import TestCase
from django.utils import timezone
from sales.tests import test_sales as sale_tests
from common.models import Activity
from cash.models import CashMovement
from customers.models import Customer, CustomerDebt, CustomerLedgerEntry
from catalogue.models import Product
from stock.models import ReturnReceipt, StockMovement
from sales.models import Sale, SalePayment, SaleReturn


class SaleReturnTests(TestCase):
    setUp = sale_tests.CashSaleTests.setUp
    authenticate = sale_tests.CashSaleTests.authenticate
    payload = sale_tests.CashSaleTests.payload
    url = sale_tests.CashSaleTests.url

    def prepare_sale(self, **changes):
        self.authenticate(self.owner)
        result = self.client.post(self.url(), self.payload(**changes), format="json")
        self.assertEqual(result.status_code, 201, result.content)
        return result.data

    def return_url(self, sale):
        return self.url(sale["id"]) + "returns/"

    def return_payload(self, sale, quantity=1000):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
                "reason": "Article retourné", "lines": [{"id": str(uuid.uuid4()), "sale_line_id": sale["lines"][0]["id"], "quantity_milli": quantity}]}

    def test_successive_returns_use_original_net_and_refund_33_34_33_without_automatic_cash_or_stock(self):
        sale = self.prepare_sale(global_discount_fcfa=14900, lines=[{"id": str(uuid.uuid4()), "product_id": str(self.product.id),
            "product_version": 1, "quantity_milli": 3000}], payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 100}])
        Product.objects.filter(id=self.product.id).update(sale_price_fcfa=9999, is_active=False)
        for expected in (33, 34, 33):
            data = self.return_payload(sale)
            response = self.client.post(self.return_url(sale), data, format="json")
            self.assertEqual(response.status_code, 201, response.content)
            self.assertEqual(response.data["amount_fcfa"], expected)
            self.assertEqual(response.data["credit_fcfa"], expected)
            self.assertEqual(response.data["debt_reduced_fcfa"], 0)
            self.assertEqual(self.client.post(self.return_url(sale), data, format="json").data, response.data)
        self.assertEqual(self.client.post(self.return_url(sale), self.return_payload(sale), format="json").status_code, 409)
        self.assertEqual(SaleReturn.objects.count(), 3)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 100)
        self.assertEqual(StockMovement.objects.filter(kind="sale").count(), 1)
        self.assertFalse(ReturnReceipt.objects.exists())
        self.assertEqual(Sale.objects.get().net_total_fcfa, 100)
        self.assertEqual(self.client.get(self.return_url(sale)).data["count"], 3)

    def test_credit_return_first_reduces_only_original_sale_debt_and_preserves_old_payments(self):
        customer = Customer.objects.create(shop=self.shop, name="Client")
        sale = self.prepare_sale(mode="credit", customer_id=str(customer.id), payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 4000}])
        data = self.return_payload(sale, 2000)
        returned = self.client.post(self.return_url(sale), data, format="json")
        self.assertEqual(returned.status_code, 201, returned.content)
        self.assertEqual(returned.data["amount_fcfa"], 10000)
        self.assertEqual(returned.data["debt_reduced_fcfa"], 6000)
        self.assertEqual(returned.data["credit_fcfa"], 4000)
        self.assertEqual(CustomerDebt.objects.get().remaining_fcfa, 0)
        self.assertEqual(SalePayment.objects.get().amount_fcfa, 4000)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 4000)
        statement = self.client.get(f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/statement/")
        self.assertEqual(statement.data["closing_balance_fcfa"], -4000)
        self.assertEqual(statement.data["closing_unallocated_fcfa"], 4000)
        self.assertEqual(self.client.post(self.return_url(sale), data, format="json").status_code, 200)
        self.assertEqual(CustomerLedgerEntry.objects.filter(kind="sale_return").count(), 1)

    def test_seller_foreign_sale_invalid_quantity_and_changed_retry_are_refused(self):
        sale = self.prepare_sale()
        data = self.return_payload(sale)
        self.authenticate(self.seller)
        self.assertEqual(self.client.post(self.return_url(sale), data, format="json").status_code, 403)
        self.assertEqual(self.client.get(self.return_url(sale)).status_code, 403)
        self.authenticate(self.owner)
        wrong = f"/api/v1/shops/{self.other.id}/sales/{sale['id']}/returns/"
        self.assertEqual(self.client.post(wrong, data, format="json").status_code, 404)
        bad = {**data, "lines": [{**data["lines"][0], "quantity_milli": 1500}]}
        self.assertEqual(self.client.post(self.return_url(sale), bad, format="json").status_code, 400)
        self.assertEqual(self.client.post(self.return_url(sale), data, format="json").status_code, 201)
        self.assertEqual(self.client.post(self.return_url(sale), {**data, "reason": "Autre décision"}, format="json").status_code, 409)
        self.assertEqual(SaleReturn.objects.count(), 1)

    def test_invalid_later_line_rolls_back_entire_return(self):
        sale = self.prepare_sale()
        data = self.return_payload(sale)
        data["lines"].append({"id": str(uuid.uuid4()), "sale_line_id": str(uuid.uuid4()), "quantity_milli": 1000})
        self.assertEqual(self.client.post(self.return_url(sale), data, format="json").status_code, 404)
        self.assertFalse(SaleReturn.objects.exists())
        self.assertFalse(Activity.objects.filter(action="sale_return_created").exists())

    def test_zero_value_return_is_valid_and_duplicate_line_or_pre_sale_date_is_refused(self):
        sale = self.prepare_sale(global_discount_fcfa=10000, payments=[])
        data = self.return_payload(sale)
        repeated = {**data, "lines": data["lines"] + [{**data["lines"][0], "id": str(uuid.uuid4())}]}
        self.assertEqual(self.client.post(self.return_url(sale), repeated, format="json").status_code, 400)
        before_sale = {**data, "occurred_at": "2026-01-01T09:00:00Z"}
        self.assertEqual(self.client.post(self.return_url(sale), before_sale, format="json").status_code, 400)
        created = self.client.post(self.return_url(sale), data, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.data["amount_fcfa"], 0)
        self.assertEqual(created.data["credit_fcfa"], 0)
        self.assertFalse(CashMovement.objects.exists())
