"""US-33: value real movement sources without replacing invoice or payment facts."""
from datetime import timedelta
from fractions import Fraction
import uuid

from django.test import TestCase
from django.utils import timezone

from common.models import Activity
from cash.models import CashMovement
from catalogue.models import Product
from purchasing.models import PurchaseDocument, PurchaseReceiptLine, SupplierDebt
from sales.models import Sale
from stock.models import StockCostSnapshot, StockMovement
from purchasing.tests import test_purchases as purchase_tests


class MovementCostTests(TestCase):
    setUp = purchase_tests.PurchaseTests.setUp
    authenticate = purchase_tests.PurchaseTests.authenticate
    url = purchase_tests.PurchaseTests.url
    payload = purchase_tests.PurchaseTests.payload

    def invoice_and_receipts(self, total_price=1000, quantity=10000, parts=(6000, 4000)):
        data = self.payload(lines=[{"id": str(uuid.uuid4()), "product_id": str(self.product.id),
            "quantity_milli": quantity, "unit_price_fcfa": total_price}])
        response = self.client.post(self.url(), data, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        invoice = response.data
        rows = []
        for index, part in enumerate(parts):
            received = self.client.post(self.url() + invoice["id"] + "/receipts/", {
                "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "occurred_at": f"2026-01-{index + 2:02d}T00:00:00Z", "reason": "Réception réelle",
                "lines": [{"line_id": invoice["lines"][0]["id"], "quantity_milli": part}],
            }, format="json")
            self.assertEqual(received.status_code, 201, received.content)
            rows.append(PurchaseReceiptLine.objects.get(receipt_id=received.data["id"]).movement)
        return invoice, rows

    def test_real_partial_receipts_use_frozen_invoice_amount_without_second_cash_or_debt(self):
        from stock.services.movement_costs import value_ordered_movements
        invoice, movements = self.invoice_and_receipts()
        Product.objects.filter(id=self.product.id).update(purchase_price_fcfa=9999)
        state, valuations = value_ordered_movements(movements)
        self.assertEqual(state.quantity_milli, 10000)
        self.assertEqual(state.average_unit_cost_micros, Fraction(1000000000))
        self.assertEqual([cost.total_nanos_fcfa for _movement, _after, cost in valuations],
            [6000000000000, 4000000000000])
        self.assertFalse(CashMovement.objects.exists())
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 10000)
        self.assertEqual(PurchaseDocument.objects.get().total_fcfa, invoice["total_fcfa"])

    def test_one_fcfa_received_in_three_parts_preserves_the_invoice_total(self):
        from stock.services.movement_costs import value_ordered_movements
        Product.objects.filter(id=self.product.id).update(unit_kind="weight", unit_label="kg")
        _invoice, movements = self.invoice_and_receipts(total_price=1, quantity=1000, parts=(333, 333, 334))
        state, valuations = value_ordered_movements(movements)
        self.assertEqual([cost.total_nanos_fcfa for _movement, _after, cost in valuations],
            [333000000, 333000000, 334000000])
        self.assertEqual(state.stock_value_nanos_fcfa, 1000000000)

    def test_ordered_opening_purchase_and_sale_freeze_1100_and_retry_without_duplicates(self):
        from stock.services.movement_costs import value_ordered_movements, freeze_movement_cost
        _invoice, incoming = self.invoice_and_receipts(total_price=1200, parts=(10000,))
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="fixture_opening", target_id=self.product.id)
        opening = StockMovement.objects.create(product=self.product, actor=self.owner, operation=event,
            kind="opening", quantity_milli=10000, unit_cost_micros=1000000000,
            occurred_at=incoming[0].occurred_at - timedelta(days=1), reason="Ouverture vérifiée")
        sale = self.client.post(f"/api/v1/shops/{self.shop.id}/sales/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1, "quantity_milli": 5000}],
            "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 500}],
        }, format="json")
        self.assertEqual(sale.status_code, 201, sale.content)
        outgoing = StockMovement.objects.get(kind="sale")
        state, valuations = value_ordered_movements([opening, *incoming, outgoing])
        movement, after, cost = valuations[-1]
        self.assertEqual(cost.total_nanos_fcfa, 5500000000000)
        self.assertEqual(cost.unit_cost_micros, Fraction(1100000000))
        self.assertEqual(state.stock_value_nanos_fcfa, 16500000000000)
        saved, created = freeze_movement_cost(movement, after, cost)
        self.assertFalse(created)  # The real sale has already frozen this valuation.
        self.assertEqual(saved.total_cost_nanos_fcfa, 5500000000000)
        self.assertEqual(saved.quantity_after_milli, 15000)
        self.assertEqual(saved.unit_cost_micros_numerator, 1100000000)
        self.assertEqual(saved.unit_cost_micros_denominator, 1)
        self.assertEqual(saved.trigger_operation_id, movement.operation_id)
        retry, created = freeze_movement_cost(movement, after, cost)
        self.assertFalse(created)
        self.assertEqual(retry.id, saved.id)
        self.assertEqual(StockCostSnapshot.objects.count(), 1)
        self.assertEqual(Sale.objects.get().net_total_fcfa, 500)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 500)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 12000)
        with self.assertRaises(ValueError):
            freeze_movement_cost(movement, after, type(cost)(0, Fraction(0), "known", 0))
        saved.refresh_from_db()
        self.assertEqual(saved.total_cost_nanos_fcfa, 5500000000000)

    def test_unknown_and_zero_openings_are_distinct_and_wrong_shop_stream_is_rejected(self):
        from stock.services.movement_costs import value_ordered_movements
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="fixture_opening", target_id=self.product.id)
        opening = StockMovement.objects.create(product=self.product, actor=self.owner, operation=event,
            kind="opening", quantity_milli=1000, occurred_at=timezone.now(), reason="Coût inconnu")
        state, values = value_ordered_movements([opening])
        self.assertIsNone(state.stock_value_nanos_fcfa)
        self.assertEqual(values[0][2].state, "unknown")
        opening.unit_cost_micros = 0
        state, values = value_ordered_movements([opening])
        self.assertEqual(state.stock_value_nanos_fcfa, 0)
        self.assertEqual(values[0][2].state, "known")
        foreign = Product.objects.create(shop=self.shop, name="Autre", unit_kind="piece", unit_label="pièce", sale_price_fcfa=100)
        other = StockMovement.objects.create(product=foreign, actor=self.owner, operation=event,
            kind="loss", quantity_milli=-1000, occurred_at=timezone.now(), reason="Autre produit")
        with self.assertRaises(ValueError):
            value_ordered_movements([opening, other])
