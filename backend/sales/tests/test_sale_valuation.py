"""US-33: actual sales freeze exact known, unknown and provisional costs."""
from fractions import Fraction
import uuid

from django.test import TestCase
from django.utils import timezone

from cash.models import CashMovement
from catalogue.models import Product
from sales.models import Sale
from stock.models import StockCostSnapshot, StockMovement
from purchasing.models import SupplierDebt
from stock.tests import test_movement_costs as cost_tests


class SaleValuationTests(TestCase):
    setUp = cost_tests.MovementCostTests.setUp
    authenticate = cost_tests.MovementCostTests.authenticate
    url = cost_tests.MovementCostTests.url
    payload = cost_tests.MovementCostTests.payload
    invoice_and_receipts = cost_tests.MovementCostTests.invoice_and_receipts

    def test_zero_quantity_opening_does_not_crash_a_real_sale_or_invent_a_weighted_average(self):
        for declared_cost in (None, "1000"):
            with self.subTest(declared_cost=declared_cost):
                result = self.client.post(f"/api/v1/shops/{self.shop.id}/products/", {
                    "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Stock nul",
                    "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 100,
                    "opening": {"quantity_milli": 0, "unit_cost_fcfa": declared_cost,
                        "occurred_at": "2026-01-01T00:00:00Z", "reason": "Aucune unité présente"},
                }, format="json")
                self.assertEqual(result.status_code, 201, result.content)
                self.product = Product.objects.get(id=result.data["id"])
                _, _, _, _sale, cost = self.sell()
                self.assertEqual(cost.cost_state, "unknown")
                self.assertIsNone(cost.total_cost_nanos_fcfa)
                self.assertEqual(cost.quantity_after_milli, -1000)

    def sell(self, quantity=1000, occurred_at=None):
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1,
                "quantity_milli": quantity}],
            "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": quantity // 10}]}
        if occurred_at is not None:
            data.update(occurred_at=occurred_at.isoformat(), reason="Date effective vérifiée")
        path = f"/api/v1/shops/{self.shop.id}/sales/"
        response = self.client.post(path, data, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        sale = Sale.objects.get(id=data["id"])
        snapshot = StockCostSnapshot.objects.get(movement__sale_line__sale=sale, version=1)
        return path, data, response, sale, snapshot

    def test_sale_reads_real_invoice_receipts_and_freezes_cost_without_cash_or_debt_duplicates(self):
        self.invoice_and_receipts(total_price=1000, parts=(10000,))
        self.invoice_and_receipts(total_price=1200, parts=(10000,))
        path, data, response, sale, cost = self.sell(5000)
        self.assertEqual(cost.total_cost_nanos_fcfa, 5500000000000)
        self.assertEqual(Fraction(cost.unit_cost_micros_numerator, cost.unit_cost_micros_denominator), Fraction(1100000000))
        self.assertEqual(cost.cost_state, "known")
        self.assertEqual(cost.quantity_after_milli, 15000)
        self.assertEqual(cost.stock_value_after_nanos_fcfa, 16500000000000)
        Product.objects.filter(id=self.product.id).update(purchase_price_fcfa=9999)
        self.assertEqual(self.client.post(path, data, format="json").data, response.data)
        cost.refresh_from_db()
        self.assertEqual(cost.total_cost_nanos_fcfa, 5500000000000)
        self.assertEqual(StockCostSnapshot.objects.count(), 1)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 500)
        self.assertEqual(sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)), 22000)
        self.assertEqual(sale.net_total_fcfa, 500)
        self.assertNotIn("unit_cost_micros", response.data["lines"][0])
        self.assertNotIn("total_cost_nanos_fcfa", response.data["lines"][0])

    def test_negative_stock_receipt_does_not_rewrite_the_previous_provisional_sale(self):
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        _, _, _, _sale, old = self.sell(3000)
        self.assertEqual(old.cost_state, "provisional")
        self.assertEqual(old.total_cost_nanos_fcfa, 3000000000000)
        # The second receipt must follow the oversale in effective time.
        _invoice, received = self.invoice_and_receipts(total_price=1200, quantity=5000, parts=(5000,))
        StockMovement.objects.filter(id=received[0].id).update(occurred_at=timezone.now())
        _, _, _, _new_sale, new = self.sell()
        self.assertEqual(new.cost_state, "known")
        self.assertEqual(new.total_cost_nanos_fcfa, 1200000000000)
        self.assertEqual(new.quantity_after_milli, 2000)
        self.assertEqual(new.stock_value_after_nanos_fcfa, 2400000000000)
        old.refresh_from_db()
        self.assertEqual(old.cost_state, "provisional")
        self.assertEqual(old.total_cost_nanos_fcfa, 3000000000000)
        self.assertEqual(StockCostSnapshot.objects.filter(movement=old.movement).count(), 1)

    def test_backdated_sale_does_not_use_a_future_receipt_and_equal_time_receipt_precedes_sale(self):
        _invoice, rows = self.invoice_and_receipts(total_price=1200, quantity=1000, parts=(1000,))
        instant = rows[0].occurred_at
        from datetime import timedelta
        _, _, _, _sale, earlier = self.sell(1000, instant - timedelta(seconds=1))
        self.assertEqual(earlier.cost_state, "unknown")
        self.assertIsNone(earlier.total_cost_nanos_fcfa)
        # This receipt covers the old deficit; receive a second unit at the same instant.
        _invoice, second = self.invoice_and_receipts(total_price=1400, quantity=1000, parts=(1000,))
        self.assertEqual(second[0].occurred_at, instant)
        _, _, _, _sale, equal = self.sell(1000, instant)
        self.assertEqual(equal.cost_state, "known")
        self.assertEqual(equal.total_cost_nanos_fcfa, 1400000000000)
        earlier.refresh_from_db()
        self.assertIsNone(earlier.total_cost_nanos_fcfa)

    def test_known_zero_and_no_known_cost_are_distinct_for_real_sales(self):
        _, _, _, _sale, missing = self.sell()
        self.assertEqual(missing.cost_state, "unknown")
        self.assertIsNone(missing.total_cost_nanos_fcfa)
        _invoice, received = self.invoice_and_receipts(total_price=0, quantity=3000, parts=(3000,))
        StockMovement.objects.filter(id=received[0].id).update(occurred_at=timezone.now())
        _, _, _, _sale, free = self.sell()
        self.assertEqual(free.cost_state, "known")
        self.assertEqual(free.total_cost_nanos_fcfa, 0)
        self.assertEqual(free.unit_cost_micros_numerator, 0)
