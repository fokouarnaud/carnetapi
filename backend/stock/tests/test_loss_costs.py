"""US-14/34: losses preserve initial exact cost and trace later fee revisions."""
from django.test import TestCase
import uuid
from cash.models import CashMovement
from stock.models import CostRevaluationItem, StockCostSnapshot, StockLoss, StockMovement
from customers.models import CustomerDebt
from catalogue.models import Product
from sales.models import Sale
from purchasing.models import SupplierDebt
from stock.tests import test_return_costs as fixtures


class LossCostTests(TestCase):
    setUp = fixtures.ReturnCostTests.setUp
    authenticate = fixtures.ReturnCostTests.authenticate
    url = fixtures.ReturnCostTests.url
    payload = fixtures.ReturnCostTests.payload
    invoice_and_receipts = fixtures.ReturnCostTests.invoice_and_receipts
    event = fixtures.ReturnCostTests.event

    def losses(self):
        return f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/losses/"

    def test_loss_freezes_1100_average_cost_without_creating_sales_cash_or_customer_debt(self):
        self.invoice_and_receipts(total_price=1000, quantity=10000, parts=(10000,))
        self.invoice_and_receipts(total_price=1200, quantity=10000, parts=(10000,))
        body = self.event(quantity_milli=5000)
        response = self.client.post(self.losses(), body, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        cost = StockCostSnapshot.objects.filter(movement__kind="loss").first()
        self.assertIsNotNone(cost)
        self.assertEqual(cost.total_cost_nanos_fcfa, 5500000000000)
        self.assertEqual(cost.quantity_after_milli, 15000)
        self.assertEqual(cost.stock_value_after_nanos_fcfa, 16500000000000)
        self.assertEqual(cost.trigger_operation_id, StockLoss.objects.get().operation_id)
        self.assertEqual(response.data["cost"]["total_cost_nanos_fcfa"], "5500000000000")
        self.assertEqual(self.client.post(self.losses(), body, format="json").data, response.data)
        self.assertEqual(StockCostSnapshot.objects.count(), 1)
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(CustomerDebt.objects.exists())
        self.assertEqual(sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)), 22000)

    def test_late_fee_revises_loss_cost_and_preserves_original_operation_receipt(self):
        invoice, _ = self.invoice_and_receipts(total_price=1000, quantity=2000, parts=(2000,))
        body = self.event(quantity_milli=1000)
        recorded = self.client.post(self.losses(), body, format="json")
        self.assertEqual(recorded.status_code, 201)
        charge = self.client.post(self.url() + invoice["id"] + "/charges/", self.event(label="Transport",
            amount_fcfa=200, allocations=[{"line_id": invoice["lines"][0]["id"], "amount_fcfa": 200}]), format="json")
        self.assertEqual(charge.status_code, 201)
        path = f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/cost-revaluations/"
        preview = self.client.get(path + "preview/")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.data["loss_cost_before"]["total_nanos_fcfa"], "1000000000000")
        self.assertEqual(preview.data["loss_cost_after"]["total_nanos_fcfa"], "1100000000000")
        applied = self.client.post(path, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "reason": "Coût de perte recontrôlé", "preview_token": preview.data["preview_token"]}, format="json")
        self.assertEqual(applied.status_code, 201, applied.content)
        current = self.client.get(self.losses() + recorded.data["id"] + "/")
        self.assertEqual(current.data["cost"]["total_cost_nanos_fcfa"], "1100000000000")
        self.assertEqual(current.data["cost"]["version"], 2)
        original = StockCostSnapshot.objects.get(movement__kind="loss", version=1)
        self.assertEqual(recorded.data["cost"]["valuation_kind"], "captured")
        self.assertEqual(current.data["cost"]["valuation_kind"], "revised")
        self.assertEqual(current.data["cost"]["supersedes_id"], str(original.id))
        self.assertEqual(original.total_cost_nanos_fcfa, 1000000000000)
        revised = StockCostSnapshot.objects.get(supersedes=original)
        self.assertTrue(CostRevaluationItem.objects.filter(previous=original, revised=revised).exists())
        self.assertEqual(self.client.post(self.losses(), body, format="json").data, recorded.data)
        self.assertEqual(StockMovement.objects.count(), 2)
        self.assertFalse(CashMovement.objects.exists())
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 2000)

    def test_unknown_zero_and_oversized_physical_loss_keep_distinct_cost_states(self):
        for price, quantity, state, total in ((None, 1000, "unknown", None),
                (0, 1000, "known", "0"), (1000, 2000, "provisional", "2000000000000")):
            with self.subTest(price=price):
                self.product = Product.objects.create(shop=self.shop, name="État de coût", unit_kind="piece",
                    unit_label="pièce", sale_price_fcfa=100)
                if price is not None:
                    self.invoice_and_receipts(total_price=price, quantity=1000, parts=(1000,))
                response = self.client.post(self.losses(), self.event(quantity_milli=quantity), format="json")
                self.assertEqual(response.status_code, 201, response.content)
                self.assertEqual(response.data["cost"]["cost_state"], state)
                self.assertEqual(response.data["cost"]["total_cost_nanos_fcfa"], total)
                if state != "known":
                    self.assertIsNone(response.data["cost"]["stock_value_after_nanos_fcfa"])
                    self.assertTrue(response.data["stock_requires_regularization"])

    def test_fractional_losses_preserve_the_one_fcfa_received_cost_across_three_outputs(self):
        Product.objects.filter(id=self.product.id).update(unit_kind="weight", unit_label="kg")
        self.invoice_and_receipts(total_price=249, quantity=3, parts=(3,))
        totals = []
        for _ in range(3):
            response = self.client.post(self.losses(), self.event(quantity_milli=1), format="json")
            self.assertEqual(response.status_code, 201, response.content)
            self.assertEqual(response.data["cost"]["unit_cost_micros_numerator"], "1000000000")
            self.assertEqual(response.data["cost"]["unit_cost_micros_denominator"], "3")
            totals.append(response.data["cost"]["total_cost_nanos_fcfa"])
        self.assertEqual(totals, ["333333333", "333333334", "333333333"])
        self.assertEqual(response.data["cost"]["stock_value_after_nanos_fcfa"], "0")
        self.assertEqual(StockCostSnapshot.objects.filter(movement__kind="loss").count(), 3)
        self.assertFalse(CashMovement.objects.exists())

    def test_large_loss_cost_remains_an_exact_json_string(self):
        self.invoice_and_receipts(total_price=9223372036854775807, quantity=1000, parts=(1000,))
        response = self.client.post(self.losses(), self.event(quantity_milli=1000), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["cost"]["total_cost_nanos_fcfa"], "9223372036854775807000000000")
        self.assertEqual(StockCostSnapshot.objects.get(movement__kind="loss").total_cost_nanos_fcfa,
            9223372036854775807000000000)

    def test_reconstructed_legacy_loss_cost_is_explicit_and_does_not_invent_an_original(self):
        self.invoice_and_receipts(total_price=1000, quantity=2000, parts=(2000,))
        response = self.client.post(self.losses(), self.event(quantity_milli=1000), format="json")
        self.assertEqual(response.status_code, 201)
        StockCostSnapshot.objects.filter(movement__kind="loss").delete()  # A loss predating frozen cost records.
        detail = self.losses() + response.data["id"] + "/"
        self.assertIsNone(self.client.get(detail).data["cost"])
        path = f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/cost-revaluations/"
        preview = self.client.get(path + "preview/")
        self.assertEqual(preview.status_code, 200)
        self.assertIsNone(preview.data["loss_cost_before"]["total_nanos_fcfa"])
        operation_id = str(uuid.uuid4())
        result = self.client.post(path, {"id": str(uuid.uuid4()), "operation_id": operation_id,
            "reason": "Historique de coût reconstitué", "preview_token": preview.data["preview_token"]}, format="json")
        self.assertEqual(result.status_code, 201, result.content)
        current = self.client.get(detail).data["cost"]
        self.assertEqual(current["version"], 1)
        self.assertEqual(current["valuation_kind"], "reconstructed")
        self.assertEqual(current["trigger_operation_id"], operation_id)
        self.assertIsNone(current["supersedes_id"])
        self.assertEqual(current["total_cost_nanos_fcfa"], "1000000000000")
        history = self.client.get(path + result.data["id"] + "/items/")
        loss = StockLoss.objects.get()
        item = next(row for row in history.data["results"] if row["movement_id"] == str(loss.movement_id))
        self.assertIsNone(item["previous"])
        self.assertIsNone(item["previous_legacy_unit_cost_micros"])
