"""US-33: explicitly close an empty stock's valuation gap, keeping its history."""
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from common.models import Activity
from cash.models import CashMovement
from identity.models import Installation
from stock.models import StockCostSnapshot, StockMovement, StockValuationAdjustment
from purchasing.models import SupplierCredit
from stock.tests import test_stock_valuations as fixtures


class EmptyStockValuationTests(TestCase):
    setUp = fixtures.StockValuationTests.setUp
    authenticate = fixtures.StockValuationTests.authenticate
    url = fixtures.StockValuationTests.url
    payload = fixtures.StockValuationTests.payload
    invoice_and_receipts = fixtures.StockValuationTests.invoice_and_receipts
    make_sale = fixtures.StockValuationTests.make_sale
    event = fixtures.StockValuationTests.event
    path = fixtures.StockValuationTests.path
    preview = fixtures.StockValuationTests.preview
    body = fixtures.StockValuationTests.body

    def empty_gap(self):
        invoice, _ = self.invoice_and_receipts(total_price=1000, parts=(10000,))
        self.invoice_and_receipts(total_price=1200, parts=(10000,))
        self.make_sale(19000)
        departure = self.client.post(self.url() + invoice["id"] + "/returns/", self.event(
            lines=[{"line_id": invoice["lines"][0]["id"], "quantity_milli": 1000}]), format="json")
        self.assertEqual(departure.status_code, 201, departure.content)
        return invoice

    def test_model_allows_empty_closure_only_with_zero_previous_and_confirmed_values(self):
        operation = Activity.objects.create(shop=self.shop, actor=self.owner, action="stock_valuation_confirmed",
            target_id=self.product.id, reason="Écart examiné sur stock vide")
        decision = StockValuationAdjustment.objects.create(product=self.product, operation=operation,
            installation=Installation.objects.get(user=self.owner), occurred_at=timezone.now(),
            stock_quantity_milli=0, previous_value_nanos_fcfa=0, value_fcfa=0, basis_fingerprint="a" * 64)
        decision.refresh_from_db()
        self.assertEqual(decision.stock_quantity_milli, 0)
        for changes in ({"stock_quantity_milli": -1}, {"previous_value_nanos_fcfa": None},
                {"previous_value_nanos_fcfa": 1}, {"value_fcfa": 1}, {"stock_quantity_milli": False}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                StockValuationAdjustment.objects.filter(id=decision.id).update(**changes)

    def test_explicit_closure_keeps_historical_gap_costs_cash_and_quantities(self):
        self.empty_gap()
        snapshots = list(StockCostSnapshot.objects.order_by("id").values())
        pending = self.client.get(self.path() + "current/")
        self.assertEqual(pending.data["quantity_milli"], "0")
        self.assertTrue(pending.data["has_unresolved_valuation"])
        preview = self.preview(0)
        self.assertEqual(preview.data["valuation_action"], "close_empty_gap")
        self.assertEqual(preview.data["previous_value_nanos_fcfa"], "0")
        body = self.body(preview, 0)
        decision = self.client.post(self.path(), body, format="json")
        self.assertEqual(decision.status_code, 201, decision.content)
        self.assertEqual(decision.data["valuation_action"], "close_empty_gap")
        self.assertEqual(self.client.post(self.path(), body, format="json").data, decision.data)
        current = self.client.get(self.path() + "current/")
        self.assertFalse(current.data["valuation_requires_regularization"])
        self.assertFalse(current.data["has_unresolved_valuation"])
        self.assertEqual(current.data["stock_value_nanos_fcfa"], "0")
        self.assertEqual(list(StockCostSnapshot.objects.order_by("id").values()), snapshots)
        gap = StockCostSnapshot.objects.get(movement__kind="supplier_return")
        self.assertEqual(gap.valuation_gap_nanos_fcfa, 100000000000)
        self.assertEqual(StockMovement.objects.count(), 4)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 1900)
        self.assertFalse(SupplierCredit.objects.exists())

    def test_empty_stock_refuses_nonzero_value_and_stale_amount_cannot_close_gap(self):
        self.empty_gap()
        self.assertEqual(self.client.post(self.path() + "preview/", {"value_fcfa": 1}, format="json").status_code, 400)
        preview = self.preview(0)
        changed = self.client.post(self.path(), {**self.body(preview, 0), "value_fcfa": 1}, format="json")
        self.assertEqual(changed.status_code, 409, changed.content)
        self.assertFalse(StockValuationAdjustment.objects.exists())
        self.assertTrue(self.client.get(self.path() + "current/").data["has_unresolved_valuation"])

    def test_late_fee_reopens_review_while_retry_preserves_original_closure(self):
        invoice = self.empty_gap()
        preview = self.preview(0)
        body = self.body(preview, 0)
        decision = self.client.post(self.path(), body, format="json")
        self.assertEqual(decision.status_code, 201, decision.content)
        fee = self.client.post(self.url() + invoice["id"] + "/charges/", self.event(label="Nouveaux frais",
            amount_fcfa=1000, allocations=[{"line_id": invoice["lines"][0]["id"], "amount_fcfa": 1000}]), format="json")
        self.assertEqual(fee.status_code, 201, fee.content)
        current = self.client.get(self.path() + "current/")
        self.assertTrue(current.data["has_unresolved_valuation"])
        self.assertEqual(current.data["invalid_adjustments_count"], 1)
        self.assertEqual(self.client.post(self.path(), body, format="json").data, decision.data)
        self.assertTrue(self.client.get(self.path() + "current/").data["has_unresolved_valuation"])
        fresh = self.preview(0)
        confirmed = self.client.post(self.path(), self.body(fresh, 0), format="json")
        self.assertEqual(confirmed.status_code, 201, confirmed.content)
        self.assertFalse(self.client.get(self.path() + "current/").data["has_unresolved_valuation"])

    def test_closure_does_not_invent_cost_for_later_stock_or_accept_negative_stock(self):
        self.empty_gap()
        preview = self.preview(0)
        decision = self.client.post(self.path(), self.body(preview, 0), format="json")
        self.assertEqual(decision.status_code, 201, decision.content)
        _, incoming = self.invoice_and_receipts(total_price=1200, quantity=1000, parts=(1000,))
        StockMovement.objects.filter(id=incoming[0].id).update(occurred_at=timezone.now())
        current = self.client.get(self.path() + "current/")
        self.assertEqual(current.data["stock_value_nanos_fcfa"], "1200000000000")
        self.assertFalse(current.data["has_unresolved_valuation"])
        self.make_sale(2000)
        self.assertEqual(self.client.post(self.path() + "preview/", {"value_fcfa": 0}, format="json").status_code, 400)
        self.assertEqual(StockValuationAdjustment.objects.count(), 1)

    def test_empty_stock_without_valuation_issue_cannot_create_a_closure(self):
        response = self.client.post(self.path() + "preview/", {"value_fcfa": 0}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(StockValuationAdjustment.objects.exists())

    def test_recomputed_costs_cannot_silently_clear_a_previously_captured_gap(self):
        invoice = self.empty_gap()
        fee = self.client.post(self.url() + invoice["id"] + "/charges/", self.event(label="Frais égalisant les coûts",
            amount_fcfa=2000, allocations=[{"line_id": invoice["lines"][0]["id"], "amount_fcfa": 2000}]), format="json")
        self.assertEqual(fee.status_code, 201, fee.content)
        # Both original purchases now cost 1200: a fresh arithmetic replay has no gap.
        # The already captured +100 still needs an explicit manager decision.
        current = self.client.get(self.path() + "current/")
        self.assertTrue(current.data["has_unresolved_valuation"])
        preview = self.preview(0)
        decision = self.client.post(self.path(), self.body(preview, 0), format="json")
        self.assertEqual(decision.status_code, 201, decision.content)
        self.assertFalse(self.client.get(self.path() + "current/").data["has_unresolved_valuation"])
        self.assertEqual(StockCostSnapshot.objects.get(movement__kind="supplier_return").valuation_gap_nanos_fcfa, 100000000000)

    def test_empty_value_closure_does_not_close_a_previous_physical_shortage(self):
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        old_sale = self.make_sale(2000)
        invoice, incoming = self.invoice_and_receipts(total_price=1000, parts=(10000,))
        StockMovement.objects.filter(id=incoming[0].id).update(occurred_at=timezone.now())
        _, incoming = self.invoice_and_receipts(total_price=1200, parts=(10000,))
        StockMovement.objects.filter(id=incoming[0].id).update(occurred_at=timezone.now())
        self.make_sale(18000)
        returned = self.client.post(self.url() + invoice["id"] + "/returns/", self.event(
            lines=[{"line_id": invoice["lines"][0]["id"], "quantity_milli": 1000}]), format="json")
        self.assertEqual(returned.status_code, 201, returned.content)
        preview = self.preview(0)
        self.assertTrue(preview.data["valuation_requires_regularization_after"])
        decision = self.client.post(self.path(), self.body(preview, 0), format="json")
        self.assertEqual(decision.status_code, 201, decision.content)
        current = self.client.get(self.path() + "current/")
        self.assertFalse(current.data["has_unresolved_valuation"])
        self.assertTrue(current.data["has_unresolved_shortage"])
        self.assertTrue(current.data["valuation_requires_regularization"])
        self.assertEqual(StockCostSnapshot.objects.get(movement__sale_line__sale_id=old_sale["id"]).cost_state, "provisional")
