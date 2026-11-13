"""US-33: physical returns retain the exact cost and uncertainty of the original sale."""
from fractions import Fraction
import uuid

from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone

from cash.models import CashMovement
from catalogue.models import Product
from stock.models import ReturnCostSnapshot, ReturnReceipt, StockCostSnapshot, StockMovement
from sales.models import SaleLine
from sales.tests import test_sale_valuation as sale_tests


class ReturnCostTests(TestCase):
    def test_same_instant_return_follows_its_sale_but_independent_receipt_precedes_sale(self):
        from datetime import datetime, timezone as utc_timezone
        instant = datetime(2026, 1, 3, tzinfo=utc_timezone.utc)
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        response = self.client.post(f"/api/v1/shops/{self.shop.id}/sales/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "occurred_at": instant.isoformat(), "reason": "Vente à sa date réelle", "backdate_reason": "Reprise historique",  # G15
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.product.id),
                "product_version": 1, "quantity_milli": 1000}],
            "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 100}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        sale = response.data
        path = self.return_path(sale, 1000)
        returned = self.client.post(path, self.event(quantity_milli=1000, condition="sellable",
            occurred_at=instant.isoformat()), format="json")
        self.assertEqual(returned.status_code, 201, returned.content)
        _, incoming = self.invoice_and_receipts(total_price=2000, quantity=1000, parts=(1000,))
        # A late independent receipt at the same effective instant.
        StockMovement.objects.filter(id=incoming[0].id).update(occurred_at=instant)
        next_sale = self.make_sale(1000)
        cost = StockCostSnapshot.objects.get(movement__sale_line__sale_id=next_sale["id"])
        # Pool 1000 + 2000, sale consumes 1500, own return adds its frozen 1000.
        self.assertEqual(cost.total_cost_nanos_fcfa, 1250000000000)
        self.assertEqual(StockCostSnapshot.objects.get(
            movement__sale_line__sale_id=sale["id"]).total_cost_nanos_fcfa, 1000000000000)

    setUp = sale_tests.SaleValuationTests.setUp
    authenticate = sale_tests.SaleValuationTests.authenticate
    url = sale_tests.SaleValuationTests.url
    payload = sale_tests.SaleValuationTests.payload
    invoice_and_receipts = sale_tests.SaleValuationTests.invoice_and_receipts

    def make_sale(self, quantity):
        amount = (quantity * self.product.sale_price_fcfa + 500) // 1000
        response = self.client.post(f"/api/v1/shops/{self.shop.id}/sales/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1,
                "quantity_milli": quantity}],
            "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": amount}] if amount else [],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return response.data

    def event(self, **values):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "occurred_at": timezone.now().isoformat(), "reason": "Contrôle physique", **values}

    def return_path(self, sale, quantity):
        root = f"/api/v1/shops/{self.shop.id}/sales/{sale['id']}/returns/"
        response = self.client.post(root, self.event(lines=[{"id": str(uuid.uuid4()),
            "sale_line_id": sale["lines"][0]["id"], "quantity_milli": quantity}]), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return root + response.data["id"] + "/lines/" + response.data["lines"][0]["id"] + "/receipts/"

    def receive(self, path, quantity=1000, condition="unsellable"):
        data = self.event(quantity_milli=quantity, condition=condition)
        response = self.client.post(path, data, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.client.post(path, data, format="json").data, response.data)
        return ReturnReceipt.objects.get(id=response.data["id"]), response

    def restock(self, path, receipt, quantity):
        response = self.client.post(path + str(receipt.id) + "/decisions/",
            self.event(kind="restock", quantity_milli=quantity, is_sellable_confirmed=True), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return response

    def test_return_cost_model_preserves_origin_exact_amount_and_database_invariants(self):
        self.invoice_and_receipts(total_price=1000, quantity=2000, parts=(2000,))
        sale = self.make_sale(2000)
        received, _response = self.receive(self.return_path(sale, 1000))
        cost = ReturnCostSnapshot.objects.get(receipt=received)
        origin = StockCostSnapshot.objects.get(movement__sale_line__sale_id=sale["id"])
        self.assertEqual(cost.sale_cost_id, origin.id)
        self.assertEqual(cost.cost_state, "known")
        self.assertEqual(cost.total_cost_nanos_fcfa, 1000000000000)
        with self.assertRaises(ProtectedError):
            origin.delete()
        for field, value in (("total_cost_nanos_fcfa", "-1"), ("total_cost_nanos_fcfa", '"0"'),
                ("unit_cost_micros_denominator", "0"), ("unit_cost_micros_numerator", "true"), ("cost_state", "unknown")):
            with self.subTest(field=field, value=value), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f'UPDATE core_returncostsnapshot SET "{field}" = %s WHERE id = %s', [value, cost.id.hex])

    def test_sellable_return_uses_original_cost_even_when_new_receipt_and_catalogue_cost_changed(self):
        self.invoice_and_receipts(total_price=1000, quantity=2000, parts=(2000,))
        sale = self.make_sale(2000)
        _invoice, incoming = self.invoice_and_receipts(total_price=1200, quantity=1000, parts=(1000,))
        StockMovement.objects.filter(id=incoming[0].id).update(occurred_at=timezone.now())
        Product.objects.filter(id=self.product.id).update(purchase_price_fcfa=9999)
        SaleLine.objects.filter(sale_id=sale["id"]).update(unit_cost_micros=9900000000)
        path = self.return_path(sale, 1000)
        received, response = self.receive(path, condition="sellable")
        self.assertEqual(response.data["unit_cost_micros"], 1000000000)
        self.assertFalse(response.data["cost_is_provisional"])
        self.assertEqual(response.data["cost"]["total_cost_nanos_fcfa"], "1000000000000")
        self.assertEqual(received.cost_snapshot.total_cost_nanos_fcfa, 1000000000000)
        next_sale = self.make_sale(1000)
        cost = StockCostSnapshot.objects.get(movement__sale_line__sale_id=next_sale["id"])
        self.assertEqual(cost.total_cost_nanos_fcfa, 1100000000000)
        self.assertEqual(CashMovement.objects.count(), 2)
        self.assertEqual(sum(CashMovement.objects.values_list("amount_fcfa", flat=True)), 300)

    def test_fractional_original_cost_is_conserved_across_sellable_and_non_sellable_receipts(self):
        Product.objects.filter(id=self.product.id).update(unit_kind="weight", unit_label="kg")
        self.invoice_and_receipts(total_price=249, quantity=3, parts=(3,))
        sale = self.make_sale(3)
        path = self.return_path(sale, 3)
        totals = []
        for condition in ("sellable", "unsellable", "unsellable"):
            received, response = self.receive(path, quantity=1, condition=condition)
            self.assertIsNone(response.data["unit_cost_micros"])
            self.assertFalse(response.data["cost_is_provisional"])
            self.assertEqual(response.data["cost"]["unit_cost_micros_numerator"], "1000000000")
            self.assertEqual(response.data["cost"]["unit_cost_micros_denominator"], "3")
            totals.append(received.cost_snapshot.total_cost_nanos_fcfa)
            if condition == "unsellable":
                decided = self.restock(path, received, 1)
                self.assertEqual(decided.data["cost"]["total_cost_nanos_fcfa"], str(totals[-1]))
        self.assertEqual(totals, [333333333, 333333334, 333333333])
        next_sale = self.make_sale(3)
        cost = StockCostSnapshot.objects.get(movement__sale_line__sale_id=next_sale["id"])
        self.assertEqual(cost.total_cost_nanos_fcfa, 1000000000)
        self.assertEqual(cost.cost_state, "known")
        self.assertFalse(CashMovement.objects.exists())

    def test_original_unknown_stays_unknown_and_known_zero_stays_known_on_receipt(self):
        sale = self.make_sale(1000)
        received, response = self.receive(self.return_path(sale, 1000), condition="sellable")
        self.assertEqual(response.data["cost"]["cost_state"], "unknown")
        self.assertIsNone(received.cost_snapshot.total_cost_nanos_fcfa)
        _invoice, incoming = self.invoice_and_receipts(total_price=0, quantity=3000, parts=(3000,))
        StockMovement.objects.filter(id=incoming[0].id).update(occurred_at=timezone.now())
        free_sale = self.make_sale(1000)
        received, response = self.receive(self.return_path(free_sale, 1000))
        self.assertEqual(response.data["cost"]["cost_state"], "known")
        self.assertEqual(received.cost_snapshot.total_cost_nanos_fcfa, 0)
        self.assertEqual(response.data["unit_cost_micros"], 0)

    def test_provisional_return_cost_propagates_through_non_sellable_restock_into_next_sale(self):
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        sale = self.make_sale(4000)
        path = self.return_path(sale, 1000)
        received, response = self.receive(path)
        self.assertEqual(response.data["cost"]["cost_state"], "provisional")
        self.assertTrue(response.data["cost_is_provisional"])
        _invoice, incoming = self.invoice_and_receipts(total_price=1200, quantity=5000, parts=(5000,))
        StockMovement.objects.filter(id=incoming[0].id).update(occurred_at=timezone.now())
        self.restock(path, received, 1000)
        next_sale = self.make_sale(1000)
        cost = StockCostSnapshot.objects.get(movement__sale_line__sale_id=next_sale["id"])
        self.assertEqual(cost.cost_state, "provisional")
        self.assertEqual(cost.total_cost_nanos_fcfa, 1133333333333)
        self.assertEqual(Fraction(cost.unit_cost_micros_numerator, cost.unit_cost_micros_denominator), Fraction(3400000000, 3))

    def test_legacy_sale_without_cost_snapshot_uses_its_frozen_integer_cost_without_inventing_an_origin(self):
        self.invoice_and_receipts(total_price=1200, quantity=1000, parts=(1000,))
        sale = self.make_sale(1000)
        # Represent a sale persisted before cost snapshots existed.
        StockCostSnapshot.objects.filter(movement__sale_line__sale_id=sale["id"]).delete()
        SaleLine.objects.filter(sale_id=sale["id"]).update(unit_cost_micros=1200000000)
        received, response = self.receive(self.return_path(sale, 1000))
        self.assertIsNone(received.cost_snapshot.sale_cost_id)
        self.assertEqual(response.data["unit_cost_micros"], 1200000000)
        self.assertEqual(received.cost_snapshot.total_cost_nanos_fcfa, 1200000000000)

    def test_cost_above_sqlite_int64_keeps_exact_strings_and_does_not_truncate_unit_cost(self):
        self.invoice_and_receipts(total_price=9223372036854775807, quantity=1000, parts=(1000,))
        sale = self.make_sale(1000)
        received, response = self.receive(self.return_path(sale, 1000))
        self.assertEqual(received.cost_snapshot.total_cost_nanos_fcfa, 9223372036854775807000000000)
        self.assertEqual(response.data["cost"]["total_cost_nanos_fcfa"], "9223372036854775807000000000")
        self.assertEqual(response.data["cost"]["unit_cost_micros_numerator"], "9223372036854775807000000")
        self.assertIsNone(response.data["unit_cost_micros"])
        self.assertFalse(response.data["cost_is_provisional"])

    def test_scrap_keeps_its_cost_share_and_partial_restocks_do_not_reintroduce_the_discarded_cost(self):
        Product.objects.filter(id=self.product.id).update(unit_kind="weight", unit_label="kg")
        self.invoice_and_receipts(total_price=249, quantity=3, parts=(3,))
        sale = self.make_sale(3)
        path = self.return_path(sale, 3)
        received, _response = self.receive(path, quantity=3)
        pool = self.client.get(f"/api/v1/shops/{self.shop.id}/unsellable-stock/")
        self.assertEqual(pool.status_code, 200)
        self.assertEqual(pool.data["results"][0]["cost"]["total_cost_nanos_fcfa"], "1000000000")
        decision_path = path + str(received.id) + "/decisions/"
        scrap = self.client.post(decision_path, self.event(kind="scrap", quantity_milli=1), format="json")
        self.assertEqual(scrap.status_code, 201, scrap.content)
        self.assertEqual(scrap.data["cost"]["total_cost_nanos_fcfa"], "333333333")
        first = self.restock(path, received, 1)
        second = self.restock(path, received, 1)
        self.assertEqual(first.data["cost"]["total_cost_nanos_fcfa"], "333333334")
        self.assertEqual(second.data["cost"]["total_cost_nanos_fcfa"], "333333333")
        next_sale = self.make_sale(2)
        cost = StockCostSnapshot.objects.get(movement__sale_line__sale_id=next_sale["id"])
        self.assertEqual(cost.total_cost_nanos_fcfa, 666666667)
        self.assertEqual(cost.cost_state, "known")
        self.assertEqual(cost.quantity_after_milli, 0)
