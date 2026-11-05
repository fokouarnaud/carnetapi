"""US-30/34: a supplier departure uses its original purchase cost, with no implicit credit."""
import uuid
from django.test import TestCase
from cash.models import CashMovement
from catalogue.models import Product
from stock.models import StockCostSnapshot, StockMovement
from purchasing.models import SupplierCredit, SupplierDebt, SupplierRefund, SupplierReturn
from stock.tests import test_return_costs as fixtures


class SupplierReturnCostTests(TestCase):
    setUp = fixtures.ReturnCostTests.setUp
    authenticate = fixtures.ReturnCostTests.authenticate
    url = fixtures.ReturnCostTests.url
    payload = fixtures.ReturnCostTests.payload
    invoice_and_receipts = fixtures.ReturnCostTests.invoice_and_receipts
    make_sale = fixtures.ReturnCostTests.make_sale
    event = fixtures.ReturnCostTests.event

    def depart(self, invoice, quantity):
        path = self.url() + invoice["id"] + "/returns/"
        body = self.event(lines=[{"line_id": invoice["lines"][0]["id"], "quantity_milli": quantity}])
        response = self.client.post(path, body, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return path, body, response

    def test_sellable_customer_return_does_not_erase_supplier_departure_history(self):
        invoice, _ = self.invoice_and_receipts(total_price=1000, quantity=4000, parts=(4000,))
        sale = self.make_sale(1000)
        self.depart(invoice, 1000)
        path = fixtures.ReturnCostTests.return_path(self, sale, 1000)
        fixtures.ReturnCostTests.receive(self, path, condition="sellable")
        _, _, response = self.depart(invoice, 1000)
        self.assertEqual(response.data["lines"][0]["cost"]["total_cost_nanos_fcfa"], "1000000000000")
        self.assertEqual(response.data["lines"][0]["cost"]["stock_value_after_nanos_fcfa"], "2000000000000")
        self.assertEqual(SupplierReturn.objects.count(), 2)
        self.assertEqual(CashMovement.objects.count(), 1)
        self.assertFalse(SupplierCredit.objects.exists())

    def test_departure_uses_original_1000_cost_and_leaves_17000_stock_value_instead_of_current_mean(self):
        invoice, _ = self.invoice_and_receipts(total_price=1000, quantity=10000, parts=(10000,))
        self.invoice_and_receipts(total_price=1200, quantity=10000, parts=(10000,))
        path, body, response = self.depart(invoice, 5000)
        from stock.services.movement_costs import ordered_product_movements, value_ordered_movements
        state, values = value_ordered_movements(ordered_product_movements(self.product.id))
        self.assertEqual(values[-1][2].total_nanos_fcfa, 5000000000000)
        self.assertEqual(state.stock_value_nanos_fcfa, 17000000000000)
        cost = StockCostSnapshot.objects.get(movement__kind="supplier_return")
        self.assertEqual(cost.total_cost_nanos_fcfa, 5000000000000)
        self.assertEqual(response.data["lines"][0]["cost"]["total_cost_nanos_fcfa"], "5000000000000")
        self.assertEqual(self.client.post(path, body, format="json").data, response.data)
        self.assertFalse(SupplierCredit.objects.exists())
        self.assertFalse(SupplierRefund.objects.exists())
        self.assertFalse(CashMovement.objects.exists())
        self.assertEqual(sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)), 22000)

    def test_late_fee_revises_departure_cost_without_changing_credit_reference_or_money(self):
        invoice, _ = self.invoice_and_receipts(total_price=1000, quantity=2000, parts=(2000,))
        path, body, recorded = self.depart(invoice, 1000)
        fee = self.client.post(self.url() + invoice["id"] + "/charges/", self.event(label="Transport", amount_fcfa=200,
            allocations=[{"line_id": invoice["lines"][0]["id"], "amount_fcfa": 200}]), format="json")
        self.assertEqual(fee.status_code, 201, fee.content)
        revaluations = f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/cost-revaluations/"
        preview = self.client.get(revaluations + "preview/")
        self.assertEqual(preview.status_code, 200, preview.content)
        applied = self.client.post(revaluations, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "reason": "Frais tardifs du retour", "preview_token": preview.data["preview_token"]}, format="json")
        self.assertEqual(applied.status_code, 201, applied.content)
        current = self.client.get(path).data["results"][0]
        self.assertEqual(current["lines"][0]["cost"]["total_cost_nanos_fcfa"], "1100000000000")
        self.assertEqual(current["lines"][0]["cost"]["version"], 2)
        self.assertEqual(StockCostSnapshot.objects.get(movement__kind="supplier_return", version=1).total_cost_nanos_fcfa, 1000000000000)
        from purchasing.api.supplier_credits import return_reference_fcfa
        self.assertEqual(return_reference_fcfa(SupplierReturn.objects.get()), 1000)
        self.assertEqual(self.client.post(path, body, format="json").data, recorded.data)
        self.assertEqual(StockMovement.objects.count(), 2)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 2000)
        self.assertFalse(CashMovement.objects.exists())

    def test_departure_exceeding_remaining_book_value_preserves_known_original_cost_and_explicit_gap(self):
        Product.objects.filter(id=self.product.id).update(unit_kind="weight", unit_label="kg")
        invoice, _ = self.invoice_and_receipts(total_price=10000, quantity=1000, parts=(1000,))
        self.invoice_and_receipts(total_price=1, quantity=100000, parts=(100000,))
        self.make_sale(100000)
        _, _, result = self.depart(invoice, 500)
        cost = result.data["lines"][0]["cost"]
        self.assertEqual(cost["total_cost_nanos_fcfa"], "5000000000000")
        self.assertEqual(cost["valuation_gap_nanos_fcfa"], "-4900000000000")
        self.assertTrue(cost["valuation_requires_regularization"])
        self.assertIsNone(cost["stock_value_after_nanos_fcfa"])
        self.assertEqual(sum(StockMovement.objects.values_list("quantity_milli", flat=True)), 500)
        self.assertFalse(SupplierCredit.objects.exists())
        self.assertEqual(CashMovement.objects.count(), 1)

    def test_three_fractional_departures_preserve_the_one_fcfa_original_purchase_cost(self):
        Product.objects.filter(id=self.product.id).update(unit_kind="weight", unit_label="kg")
        invoice, _ = self.invoice_and_receipts(total_price=249, quantity=3, parts=(3,))
        costs = []
        for _ in range(3):
            _, _, result = self.depart(invoice, 1)
            cost = result.data["lines"][0]["cost"]
            self.assertEqual(cost["unit_cost_micros_numerator"], "1000000000")
            self.assertEqual(cost["unit_cost_micros_denominator"], "3")
            self.assertFalse(cost["valuation_requires_regularization"])
            costs.append(cost["total_cost_nanos_fcfa"])
        self.assertEqual(costs, ["333333333", "333333334", "333333333"])
        self.assertEqual(cost["stock_value_after_nanos_fcfa"], "0")
        self.assertFalse(CashMovement.objects.exists())

    def test_empty_stock_keeps_positive_residual_visible_without_an_implicit_expense_or_credit(self):
        invoice, _ = self.invoice_and_receipts(total_price=1000, quantity=10000, parts=(10000,))
        self.invoice_and_receipts(total_price=1200, quantity=10000, parts=(10000,))
        self.make_sale(19000)
        _, _, result = self.depart(invoice, 1000)
        cost = result.data["lines"][0]["cost"]
        self.assertEqual(cost["quantity_after_milli"], "0")
        self.assertEqual(cost["stock_value_after_nanos_fcfa"], "0")
        self.assertEqual(cost["valuation_gap_nanos_fcfa"], "100000000000")
        self.assertTrue(cost["valuation_requires_regularization"])
        self.assertFalse(SupplierCredit.objects.exists())
        self.assertEqual(CashMovement.objects.count(), 1)
