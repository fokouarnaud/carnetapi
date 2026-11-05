"""US-33/34: valuation revisions preserve the original cost and commercial facts."""
import uuid
from django.db import IntegrityError, transaction, connection
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from sales.tests import test_sales as sale_tests
from common.models import Activity
from cash.models import CashMovement
from sales.models import Sale, SalePayment
from stock.models import StockCostSnapshot, StockMovement


class CostSnapshotTests(TestCase):
    def test_signed_valuation_gap_is_exact_and_requires_an_explicit_pending_marker(self):
        movement, initial = self.prepare_original()
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="cost_recalculated",
            target_id=movement.product_id, reason="Écart de valorisation conservé")
        revised = StockCostSnapshot.objects.create(movement=movement, trigger_operation=event, version=2, supersedes=initial,
            cost_state="known", total_cost_nanos_fcfa=5000000000000, unit_cost_micros_numerator=10000000000,
            unit_cost_micros_denominator=1, quantity_after_milli=500, stock_value_after_nanos_fcfa=None,
            valuation_requires_regularization=True, valuation_gap_nanos_fcfa=-4900000000000)
        revised.refresh_from_db()
        self.assertEqual(revised.valuation_gap_nanos_fcfa, -4900000000000)
        for changes in ({"valuation_requires_regularization": False}, {"valuation_gap_nanos_fcfa": 0},
                {"valuation_gap_nanos_fcfa": True}, {"valuation_gap_nanos_fcfa": "-4900000000000"},
                {"valuation_gap_nanos_fcfa": -0.5}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                StockCostSnapshot.objects.filter(id=revised.id).update(**changes)

    setUp = sale_tests.CashSaleTests.setUp
    authenticate = sale_tests.CashSaleTests.authenticate
    payload = sale_tests.CashSaleTests.payload
    url = sale_tests.CashSaleTests.url

    def prepare_original(self):
        StockMovement.objects.filter(kind="opening").update(unit_cost_micros=None)
        self.assertEqual(self.client.post(self.url(), self.payload(), format="json").status_code, 201)
        movement = StockMovement.objects.get(kind="sale")
        snapshot = StockCostSnapshot.objects.get(movement=movement, version=1)
        self.assertEqual(snapshot.cost_state, "unknown")
        self.assertIsNone(snapshot.total_cost_nanos_fcfa)
        return movement, snapshot

    def test_later_known_cost_is_a_new_revision_without_changing_original_price_cash_or_cost(self):
        movement, initial = self.prepare_original()
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="cost_recalculated", target_id=movement.product_id, reason="Coût initial vérifié")
        revised = StockCostSnapshot.objects.create(movement=movement, trigger_operation=event, version=2, supersedes=initial,
            cost_state="known", total_cost_nanos_fcfa=4000000000000, unit_cost_micros_numerator=2000000000,
            unit_cost_micros_denominator=1, quantity_after_milli=8000, stock_value_after_nanos_fcfa=16000000000000)
        initial.refresh_from_db()
        self.assertIsNone(initial.total_cost_nanos_fcfa)
        self.assertEqual(initial.cost_state, "unknown")
        self.assertEqual(revised.supersedes_id, initial.id)
        self.assertEqual(Sale.objects.get().net_total_fcfa, 10000)
        self.assertEqual(SalePayment.objects.count(), 2)
        self.assertEqual(CashMovement.objects.count(), 2)
        self.assertEqual(StockMovement.objects.count(), 2)
        with self.assertRaises(ProtectedError):
            initial.delete()
        with self.assertRaises(ProtectedError):
            movement.delete()
        with self.assertRaises(IntegrityError), transaction.atomic():
            StockCostSnapshot.objects.create(movement=movement, trigger_operation=event, version=3, supersedes=initial,
                cost_state="unknown", quantity_after_milli=8000, stock_value_after_nanos_fcfa=None)

    def test_known_zero_is_distinct_from_unknown_and_large_exact_values_survive_sqlite(self):
        movement, initial = self.prepare_original()
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="cost_recalculated", target_id=movement.product_id, reason="Coût nul justifié")
        zero = StockCostSnapshot.objects.create(movement=movement, trigger_operation=event, version=2, supersedes=initial,
            cost_state="known", total_cost_nanos_fcfa=0, unit_cost_micros_numerator=0, unit_cost_micros_denominator=1,
            quantity_after_milli=8000, stock_value_after_nanos_fcfa=0)
        zero.refresh_from_db()
        self.assertEqual(zero.total_cost_nanos_fcfa, 0)
        very_large = 9223372036854775807 * 1000000000
        StockCostSnapshot.objects.filter(id=zero.id).update(total_cost_nanos_fcfa=very_large,
            stock_value_after_nanos_fcfa=very_large, quantity_after_milli=9223372036854775808,
            unit_cost_micros_numerator=very_large // 2000, unit_cost_micros_denominator=1)
        zero.refresh_from_db()
        self.assertEqual(zero.total_cost_nanos_fcfa, very_large)
        self.assertEqual(zero.stock_value_after_nanos_fcfa, very_large)
        self.assertEqual(zero.quantity_after_milli, 9223372036854775808)

    def test_database_rejects_cost_state_type_and_revision_inconsistencies(self):
        _, initial = self.prepare_original()
        for changes in ({"cost_state": "known"}, {"cost_state": "other"}, {"total_cost_nanos_fcfa": "0"},
                        {"quantity_after_milli": "1.5"}, {"quantity_after_milli": "true"},
                        {"stock_value_after_nanos_fcfa": "-1"}, {"version": 0}, {"version": 2},
                        {"supersedes_id": initial.id.hex}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                field, value = next(iter(changes.items()))
                with connection.cursor() as cursor:
                    cursor.execute(f'UPDATE core_stockcostsnapshot SET "{field}" = %s WHERE id = %s', [value, initial.id.hex])

    def test_known_cost_pair_rejects_zero_denominator_negative_money_and_json_strings(self):
        movement, initial = self.prepare_original()
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="cost_recalculated", target_id=movement.product_id, reason="Valeur connue")
        known = StockCostSnapshot.objects.create(movement=movement, trigger_operation=event, version=2, supersedes=initial,
            cost_state="provisional", total_cost_nanos_fcfa=1000000000000, unit_cost_micros_numerator=1000000000,
            unit_cost_micros_denominator=1, quantity_after_milli=-1000, stock_value_after_nanos_fcfa=None)
        for changes in ({"total_cost_nanos_fcfa": "-1"}, {"total_cost_nanos_fcfa": '"0"'},
                        {"unit_cost_micros_numerator": "-1"}, {"unit_cost_micros_numerator": "true"}, {"unit_cost_micros_numerator": "0"},
                        {"unit_cost_micros_denominator": "0"}, {"unit_cost_micros_denominator": "1.5"},
                        {"stock_value_after_nanos_fcfa": "0"}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                field, value = next(iter(changes.items()))
                with connection.cursor() as cursor:
                    cursor.execute(f'UPDATE core_stockcostsnapshot SET "{field}" = %s WHERE id = %s', [value, known.id.hex])
