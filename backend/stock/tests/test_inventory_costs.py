"""US-13/33/34: explicit inventory corrections retain exact, reviewable costs."""
import uuid
from django.test import TestCase
from cash.models import CashMovement
from customers.models import CustomerDebt
from catalogue.models import Product
from sales.models import Sale
from identity.models import Shop
from stock.models import StockCostSnapshot, StockMovement
from purchasing.models import SupplierDebt
from stock.tests import test_return_costs as fixtures


class InventoryCostTests(TestCase):
    setUp = fixtures.ReturnCostTests.setUp
    authenticate = fixtures.ReturnCostTests.authenticate
    url = fixtures.ReturnCostTests.url
    payload = fixtures.ReturnCostTests.payload
    invoice_and_receipts = fixtures.ReturnCostTests.invoice_and_receipts
    event = fixtures.ReturnCostTests.event

    def correct(self, physical, expected):
        root = f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/stock-counts/"
        count = self.client.post(root, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "physical_milli": physical, "counted_at": "2026-01-03T10:00:00Z", "reason": "Inventaire réel"}, format="json")
        self.assertEqual(count.status_code, 201, count.content)
        detail = root + count.data["id"] + "/"
        body = {"operation_id": str(uuid.uuid4()), "expected_milli": expected,
            "applied_adjustment_milli": 0, "reason": "Écart physique confirmé"}
        result = self.client.post(detail + "confirm/", body, format="json")
        self.assertEqual(result.status_code, 201, result.content)
        self.assertEqual(self.client.post(detail + "confirm/", body, format="json").data, result.data)
        return detail, body, result

    def test_negative_correction_freezes_cump_without_sale_or_financial_effect(self):
        self.invoice_and_receipts(total_price=1000, parts=(10000,))
        self.invoice_and_receipts(total_price=1200, parts=(10000,))
        detail, _, result = self.correct(18000, 20000)
        cost = result.data["cost"]
        self.assertEqual(cost["total_cost_nanos_fcfa"], "2200000000000")
        self.assertEqual(cost["stock_value_after_nanos_fcfa"], "19800000000000")
        self.assertEqual(cost["valuation_kind"], "captured")
        self.assertEqual(StockCostSnapshot.objects.filter(movement__kind="inventory_adjustment").count(), 1)
        self.assertEqual(self.client.get(detail + "adjustments/").data["results"][0]["cost"], cost)
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(CustomerDebt.objects.exists())
        self.assertEqual(sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)), 22000)

    def test_positive_correction_uses_effective_cump_instead_of_catalogue_or_future_receipt(self):
        self.invoice_and_receipts(total_price=1000, parts=(10000,))
        _, receipts = self.invoice_and_receipts(total_price=1200, parts=(10000,))
        Product.objects.filter(id=self.product.id).update(purchase_price_fcfa=9999)
        StockMovement.objects.filter(id=receipts[0].id).update(occurred_at="2026-01-04T00:00:00Z")
        _, _, result = self.correct(12000, 10000)
        cost = result.data["cost"]
        self.assertEqual(cost["total_cost_nanos_fcfa"], "2000000000000")
        self.assertEqual(cost["stock_value_after_nanos_fcfa"], "12000000000000")
        self.assertEqual(cost["quantity_after_milli"], "12000")
        self.assertEqual(cost["cost_state"], "known")
        self.assertFalse(CashMovement.objects.exists())

    def test_positive_correction_without_current_cump_retains_unknown_not_zero(self):
        _, _, result = self.correct(1000, 0)
        self.assertEqual(result.data["cost"]["cost_state"], "unknown")
        self.assertIsNone(result.data["cost"]["total_cost_nanos_fcfa"])
        self.assertIsNone(result.data["cost"]["stock_value_after_nanos_fcfa"])
        self.assertTrue(result.data["cost"]["valuation_requires_regularization"])
        self.assertFalse(SupplierDebt.objects.exists())
        self.assertFalse(CashMovement.objects.exists())

    def test_same_instant_correction_uses_cump_after_supplier_departure(self):
        invoice, _ = self.invoice_and_receipts(total_price=1000, parts=(10000,))
        self.invoice_and_receipts(total_price=1200, parts=(10000,))
        departure = self.client.post(self.url() + invoice["id"] + "/returns/", self.event(
            occurred_at="2026-01-03T10:00:00Z", lines=[{"line_id": invoice["lines"][0]["id"],
                "quantity_milli": 5000}]), format="json")
        self.assertEqual(departure.status_code, 201, departure.content)
        _, _, correction = self.correct(16000, 15000)
        cost = correction.data["cost"]
        self.assertEqual(cost["total_cost_nanos_fcfa"], "1133333333333")
        self.assertEqual(cost["unit_cost_micros_numerator"], "3400000000")
        self.assertEqual(cost["unit_cost_micros_denominator"], "3")
        self.assertEqual(cost["quantity_after_milli"], "16000")
        self.assertEqual(cost["stock_value_after_nanos_fcfa"], "18133333333333")

    def test_fractional_positive_correction_preserves_nano_rounding(self):
        Product.objects.filter(id=self.product.id).update(unit_kind="weight", unit_label="kg")
        self.invoice_and_receipts(total_price=249, quantity=3, parts=(3,))
        _, _, result = self.correct(4, 3)
        self.assertEqual(result.data["cost"]["total_cost_nanos_fcfa"], "333333333")
        self.assertEqual(result.data["cost"]["stock_value_after_nanos_fcfa"], "1333333333")

    def test_known_free_stock_stays_known_on_positive_inventory_correction(self):
        self.invoice_and_receipts(total_price=0, quantity=2000, parts=(2000,))
        _, _, result = self.correct(3000, 2000)
        self.assertEqual(result.data["cost"]["cost_state"], "known")
        self.assertEqual(result.data["cost"]["total_cost_nanos_fcfa"], "0")
        self.assertEqual(result.data["cost"]["stock_value_after_nanos_fcfa"], "0")
        self.assertFalse(result.data["cost"]["valuation_requires_regularization"])

    def test_late_fee_revises_correction_without_rewriting_initial_receipt_or_physical_count(self):
        invoice, _ = self.invoice_and_receipts(total_price=1000, quantity=2000, parts=(2000,))
        detail, body, original = self.correct(3000, 2000)
        fee = self.client.post(self.url() + invoice["id"] + "/charges/", self.event(label="Transport",
            amount_fcfa=200, allocations=[{"line_id": invoice["lines"][0]["id"], "amount_fcfa": 200}]), format="json")
        self.assertEqual(fee.status_code, 201, fee.content)
        path = f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/cost-revaluations/"
        preview = self.client.get(path + "preview/")
        self.assertEqual(preview.status_code, 200, preview.content)
        revised = self.client.post(path, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "reason": "Révision après frais tardifs", "preview_token": preview.data["preview_token"]}, format="json")
        self.assertEqual(revised.status_code, 201, revised.content)
        current = self.client.get(detail + "adjustments/")
        self.assertEqual(current.status_code, 200, current.content)
        cost = current.data["results"][0]["cost"]
        self.assertEqual(cost["total_cost_nanos_fcfa"], "1100000000000")
        self.assertEqual(cost["version"], 2)
        self.assertEqual(cost["supersedes_id"], original.data["cost"]["id"])
        self.assertEqual(self.client.post(detail + "confirm/", body, format="json").data, original.data)
        self.assertEqual(self.client.get(detail).data["physical_milli"], 3000)
        self.assertEqual(StockMovement.objects.count(), 2)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 2000)
        self.assertFalse(CashMovement.objects.exists())

    def test_zero_correction_has_no_cost_and_cost_history_is_manager_only(self):
        detail, _, result = self.correct(0, 0)
        self.assertIsNone(result.data["cost"])
        self.assertFalse(StockCostSnapshot.objects.exists())
        self.assertIsNone(self.client.get(detail + "adjustments/").data["results"][0]["cost"])
        foreign_shop = Shop.objects.create(owner=self.owner, name="Autre boutique")
        foreign_path = detail.replace(str(self.shop.id), str(foreign_shop.id))
        self.assertEqual(self.client.get(foreign_path + "adjustments/").status_code, 404)
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(detail + "adjustments/").status_code, 403)
