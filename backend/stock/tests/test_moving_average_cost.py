"""US-33: exact ordered cost calculations, with explicit unknown and provisional states."""
from fractions import Fraction
from django.test import SimpleTestCase


class MovingAverageCostTests(SimpleTestCase):
    def test_supplier_departure_at_original_cost_changes_remaining_average_without_changing_its_source_cost(self):
        state = self.empty().receive(10000, 10000000000000).receive(10000, 12000000000000)
        after, cost = state.remove_at_cost(5000, 5000000000000)
        self.assertEqual(cost.total_nanos_fcfa, 5000000000000)
        self.assertEqual(cost.unit_cost_micros, Fraction(1000000000))
        self.assertEqual(after.quantity_milli, 15000)
        self.assertEqual(after.stock_value_nanos_fcfa, 17000000000000)
        self.assertEqual(after.average_unit_cost_micros, Fraction(3400000000, 3))
        self.assertFalse(after.has_unresolved_valuation)

    def test_original_cost_exceeding_remaining_value_keeps_gap_and_unknown_stock_until_review(self):
        state = self.empty().receive(1000, 10000000000000).receive(100000, 100000000000)
        remaining, _ = state.remove(100000)
        self.assertEqual(remaining.stock_value_nanos_fcfa, 100000000000)
        after, cost = remaining.remove_at_cost(500, 5000000000000)
        self.assertEqual(cost.total_nanos_fcfa, 5000000000000)
        self.assertEqual(cost.valuation_gap_nanos_fcfa, -4900000000000)
        self.assertIsNone(after.stock_value_nanos_fcfa)
        self.assertTrue(after.has_unresolved_valuation)
        empty, unknown = after.remove(500)
        self.assertEqual(unknown.state, "unknown")
        fresh = empty.receive(1000, 1000000000)
        self.assertEqual(fresh.stock_value_nanos_fcfa, 1000000000)
        self.assertTrue(fresh.has_unresolved_valuation)

    def test_empty_stock_retains_nonzero_value_difference_as_an_explicit_gap(self):
        state = self.empty().receive(10000, 10000000000000).receive(10000, 12000000000000)
        remaining, _ = state.remove(19000)
        empty, cost = remaining.remove_at_cost(1000, 1000000000000)
        self.assertEqual(empty.quantity_milli, 0)
        self.assertEqual(empty.stock_value_nanos_fcfa, 0)
        self.assertEqual(cost.valuation_gap_nanos_fcfa, 100000000000)
        self.assertTrue(empty.has_unresolved_valuation)

    def test_provisional_received_cost_stays_provisional_until_the_pool_is_depleted(self):
        state = self.empty().receive(2000, 2000000000000, is_provisional=True)
        mixed = state.receive(1000, 1200000000000)
        after, cost = mixed.remove(1000)
        self.assertEqual(cost.state, "provisional")
        self.assertEqual(cost.provisional_quantity_milli, 0)
        self.assertTrue(after.bucket_cost_is_provisional)
        empty, _cost = after.remove(2000)
        fresh = empty.receive(1000, 1400000000000)
        _, fresh_cost = fresh.remove(1000)
        self.assertEqual(fresh_cost.state, "known")

    def empty(self):
        from stock.services.moving_average_cost import AverageCostState
        return AverageCostState()

    def test_ten_at_1000_then_ten_at_1200_give_1100_and_unchanged_average_on_sales(self):
        original = self.empty().receive(10000, 10000000000000)
        state = original.receive(10000, 12000000000000)
        self.assertEqual(original.quantity_milli, 10000)
        self.assertEqual(state.average_unit_cost_micros, Fraction(1100000000))
        self.assertEqual(state.stock_value_nanos_fcfa, 22000000000000)
        after, cost = state.remove(5000)
        self.assertEqual(cost.total_nanos_fcfa, 5500000000000)
        self.assertEqual(cost.state, "known")
        self.assertEqual(after.quantity_milli, 15000)
        self.assertEqual(after.stock_value_nanos_fcfa, 16500000000000)
        self.assertEqual(after.average_unit_cost_micros, Fraction(1100000000))

    def test_unknown_cost_is_not_zero_and_known_free_stock_stays_distinct(self):
        unknown = self.empty().receive(1000, None)
        mixed = unknown.receive(1000, 1000000000000)
        self.assertIsNone(mixed.average_unit_cost_micros)
        after, cost = mixed.remove(1000)
        self.assertIsNone(cost.total_nanos_fcfa)
        self.assertEqual(cost.state, "unknown")
        empty, _cost = after.remove(1000)
        self.assertEqual(empty.stock_value_nanos_fcfa, 0)
        known = empty.receive(1000, 0)
        self.assertEqual(known.average_unit_cost_micros, Fraction(0))
        _, free = known.remove(1000)
        self.assertEqual(free.total_nanos_fcfa, 0)
        self.assertEqual(free.state, "known")

    def test_oversale_uses_last_known_average_provisionally_without_erasing_shortage(self):
        state = self.empty().receive(1000, 1000000000000)
        after, cost = state.remove(3000)
        self.assertEqual(cost.total_nanos_fcfa, 3000000000000)
        self.assertEqual(cost.state, "provisional")
        self.assertEqual(cost.provisional_quantity_milli, 2000)
        self.assertEqual(after.quantity_milli, -2000)
        self.assertTrue(after.has_unresolved_shortage)
        self.assertIsNone(after.stock_value_nanos_fcfa)
        deeper, next_cost = after.remove(1000)
        self.assertEqual(next_cost.total_nanos_fcfa, 1000000000000)
        self.assertEqual(next_cost.state, "provisional")
        self.assertEqual(deeper.quantity_milli, -3000)
        _, missing = self.empty().remove(1000)
        self.assertIsNone(missing.total_nanos_fcfa)
        self.assertEqual(missing.state, "unknown")

    def test_cumulative_cost_allocation_preserves_even_one_nano_over_successive_outflows(self):
        state = self.empty().receive(3, 1)
        amounts = []
        for _index in range(3):
            state, cost = state.remove(1)
            amounts.append(cost.total_nanos_fcfa)
        self.assertEqual(amounts, [0, 1, 0])
        self.assertEqual(state.stock_value_nanos_fcfa, 0)
        self.assertEqual(state.last_known_unit_cost_micros, Fraction(1, 3))

    def test_partial_receipt_allocation_preserves_invoice_total_and_half_up(self):
        from stock.services.moving_average_cost import receipt_cost_nanos
        self.assertEqual([receipt_cost_nanos(1, 3000, prior, 1000) for prior in (0, 1000, 2000)],
            [333333333, 333333334, 333333333])
        self.assertEqual(receipt_cost_nanos(125, 500, 0, 125), 31250000000)
        self.assertEqual(receipt_cost_nanos(9223372036854775807, 1000, 0, 1000), 9223372036854775807000000000)

    def test_fractional_or_boolean_inputs_are_refused(self):
        from stock.services.moving_average_cost import receipt_cost_nanos
        for quantity in (0, -1, 1.5, True):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                self.empty().receive(quantity, 0)
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                self.empty().remove(quantity)
        for value in (-1, 1.5, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.empty().receive(1000, value)
        with self.assertRaises(ValueError):
            receipt_cost_nanos(1, 1000, 500, 501)

    def test_restocked_return_uses_original_sale_cost_and_preserves_unknown(self):
        from stock.services.moving_average_cost import returned_cost_nanos
        self.assertEqual(returned_cost_nanos(24000000000000, 24000, 0, 3000), 3000000000000)
        self.assertEqual([returned_cost_nanos(1, 3, before, 1) for before in (0, 1, 2)], [0, 1, 0])
        self.assertIsNone(returned_cost_nanos(None, 1000, 0, 1000))
        self.assertEqual(returned_cost_nanos(0, 1000, 0, 1000), 0)
        with self.assertRaises(ValueError):
            returned_cost_nanos(1, 1000, 500, 501)

    def test_approved_receipt_after_negative_stock_prices_only_remaining_units_at_new_cost(self):
        negative, old_cost = self.empty().receive(1000, 1000000000000).remove(3000)
        after = negative.receive(5000, 6000000000000)
        self.assertEqual(after.quantity_milli, 3000)
        self.assertEqual(after.average_unit_cost_micros, Fraction(1200000000))
        self.assertEqual(after.stock_value_nanos_fcfa, 3600000000000)
        self.assertTrue(after.has_unresolved_shortage)
        self.assertEqual(old_cost.total_nanos_fcfa, 3000000000000)
        self.assertEqual(old_cost.state, "provisional")
        remaining, new_cost = after.remove(1000)
        self.assertEqual(new_cost.total_nanos_fcfa, 1200000000000)
        self.assertEqual(new_cost.state, "known")
        self.assertTrue(remaining.has_unresolved_shortage)

    def test_partial_or_exact_shortage_cover_does_not_invent_a_positive_stock_average(self):
        negative, _cost = self.empty().receive(1000, 1000000000000).remove(3000)
        partial = negative.receive(1000, 1200000000000)
        self.assertEqual(partial.quantity_milli, -1000)
        self.assertIsNone(partial.average_unit_cost_micros)
        self.assertIsNone(partial.stock_value_nanos_fcfa)
        self.assertEqual(partial.last_known_unit_cost_micros, Fraction(1200000000))
        zero = partial.receive(1000, 1300000000000)
        self.assertEqual(zero.quantity_milli, 0)
        self.assertEqual(zero.stock_value_nanos_fcfa, 0)
        self.assertTrue(zero.has_unresolved_shortage)
        self.assertIsNone(zero.average_unit_cost_micros)
        positive = zero.receive(1000, 1400000000000)
        self.assertEqual(positive.average_unit_cost_micros, Fraction(1400000000))
        self.assertTrue(positive.has_unresolved_shortage)

    def test_unknown_receipt_after_shortage_preserves_unknown_and_known_zero_remains_known(self):
        negative, _cost = self.empty().remove(1000)
        unknown = negative.receive(2000, None)
        self.assertIsNone(unknown.average_unit_cost_micros)
        self.assertIsNone(unknown.stock_value_nanos_fcfa)
        free = negative.receive(2000, 0)
        self.assertEqual(free.quantity_milli, 1000)
        self.assertEqual(free.average_unit_cost_micros, Fraction(0))
        self.assertEqual(free.stock_value_nanos_fcfa, 0)
        self.assertTrue(free.has_unresolved_shortage)

    def test_new_shortage_uses_latest_known_receipt_even_when_it_did_not_cover_the_deficit(self):
        negative, old_cost = self.empty().receive(1000, 1000000000000).remove(3000)
        partial = negative.receive(1000, 1200000000000)
        _, new_cost = partial.remove(1000)
        self.assertEqual(new_cost.total_nanos_fcfa, 1200000000000)
        self.assertEqual(new_cost.state, "provisional")
        zero = partial.receive(1000, 1300000000000)
        _, after_zero = zero.remove(1000)
        self.assertEqual(after_zero.total_nanos_fcfa, 1300000000000)
        self.assertEqual(after_zero.state, "provisional")
        self.assertEqual(old_cost.total_nanos_fcfa, 3000000000000)
        unknown = partial.receive(1000, None)
        _, after_unknown = unknown.remove(1000)
        self.assertEqual(after_unknown.total_nanos_fcfa, 1200000000000)
