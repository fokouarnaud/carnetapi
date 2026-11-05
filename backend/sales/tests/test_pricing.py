from dataclasses import FrozenInstanceError
from itertools import permutations
import uuid

from django.test import SimpleTestCase

from sales.services.pricing import LineAmount, PricedBasket, PricingFailure, ReferenceAmount, line_reference_amount, price_basket


class PricingTests(SimpleTestCase):
    def line(self, number, reference, own_discount=0):
        return LineAmount(uuid.UUID(int=number), reference, own_discount)

    def test_global_discount_is_proportional_after_line_discounts(self):
        result = price_basket([self.line(1, 2500, 500), self.line(2, 8000)], 500, None)
        self.assertIsInstance(result, PricedBasket)
        self.assertEqual([line.global_discount_fcfa for line in result.lines], [100, 400])
        self.assertEqual([line.net_fcfa for line in result.lines], [1900, 7600])
        self.assertEqual(result.reference_total_fcfa, 10500)
        self.assertEqual(result.discount_total_fcfa, 1000)
        self.assertEqual(result.net_total_fcfa, 9500)

    def test_equal_remainders_use_uuid_order_and_preserve_exact_ticket_total(self):
        result = price_basket([self.line(3, 1000), self.line(1, 1000), self.line(2, 1000)], 100, None)
        self.assertIsInstance(result, PricedBasket)
        self.assertEqual({line.line_id.int: line.global_discount_fcfa for line in result.lines}, {1: 34, 2: 33, 3: 33})
        self.assertEqual(result.net_total_fcfa, 2900)

    def test_largest_fraction_receives_remainder_before_uuid_tie_break(self):
        result = price_basket([self.line(1, 1), self.line(2, 2), self.line(3, 3)], 2, None)
        self.assertIsInstance(result, PricedBasket)
        self.assertEqual([line.global_discount_fcfa for line in result.lines], [0, 1, 1])
        self.assertEqual(result.net_total_fcfa, 4)

    def test_large_amounts_do_not_turn_unequal_remainders_into_a_float_tie(self):
        result = price_basket([self.line(2, 10000000000000001), self.line(1, 10000000000000000)], 1, None)
        self.assertIsInstance(result, PricedBasket)
        self.assertEqual([line.global_discount_fcfa for line in result.lines], [1, 0])

    def test_line_and_global_discounts_cannot_bypass_seller_cap(self):
        allowed = price_basket([self.line(1, 10000, 400)], 100, 500)
        self.assertIsInstance(allowed, PricedBasket)
        self.assertEqual(allowed.net_total_fcfa, 9500)
        denied = price_basket([self.line(1, 10000, 400)], 101, 500)
        self.assertIsInstance(denied, PricingFailure)
        self.assertEqual(denied.code, "discount_cap_exceeded")
        self.assertEqual(denied.line_ids, (uuid.UUID(int=1),))

    def test_rounding_does_not_grant_a_discount_over_the_line_cap(self):
        result = price_basket([self.line(1, 10), self.line(2, 10), self.line(3, 10)], 1, 500)
        self.assertIsInstance(result, PricingFailure)
        self.assertEqual(result.code, "discount_cap_exceeded")

    def test_zero_value_lines_do_not_receive_global_discount(self):
        result = price_basket([self.line(1, 0), self.line(2, 1000, 1000), self.line(3, 1000)], 100, None)
        self.assertIsInstance(result, PricedBasket)
        self.assertEqual([line.global_discount_fcfa for line in result.lines], [0, 0, 100])
        self.assertEqual(result.net_total_fcfa, 900)
        self.assertIsInstance(price_basket([self.line(1, 0)], 0, None), PricedBasket)
        self.assertEqual(price_basket([self.line(1, 0)], 1, None).code, "discount_exceeds_total")

    def test_discount_permission_defaults_to_zero_and_manager_can_allow_free_sale(self):
        self.assertIsInstance(price_basket([self.line(1, 1000)]), PricedBasket)
        self.assertEqual(price_basket([self.line(1, 1000, 1)]).code, "discount_cap_exceeded")
        result = price_basket([self.line(1, 1000)], 1000, None)
        self.assertIsInstance(result, PricedBasket)
        self.assertEqual(result.net_total_fcfa, 0)

    def test_invalid_and_overflow_amounts_return_typed_failures(self):
        cases = [
            ([self.line(1, -1)], 0, None, "invalid_line_amount"),
            ([self.line(1, True)], 0, None, "invalid_line_amount"),
            ([self.line(1, 1.5)], 0, None, "invalid_line_amount"),
            ([self.line(1, 10, 11)], 0, None, "discount_exceeds_line"),
            ([self.line(1, 10)], -1, None, "invalid_global_discount"),
            ([self.line(1, 10)], True, None, "invalid_global_discount"),
            ([self.line(1, 10)], 0, 10001, "invalid_discount_cap"),
            ([], 0, None, "empty_basket"),
            ([self.line(1, 10), self.line(1, 10)], 0, None, "duplicate_line_id"),
            ([self.line(1, 9223372036854775807), self.line(2, 1)], 0, None, "amount_out_of_range"),
        ]
        for lines, discount, cap, expected in cases:
            with self.subTest(expected=expected):
                result = price_basket(lines, discount, cap)
                self.assertIsInstance(result, PricingFailure)
                self.assertEqual(result.code, expected)

    def test_permutation_does_not_change_discount_per_uuid(self):
        lines = [self.line(1, 1000), self.line(2, 1000), self.line(3, 1000)]
        for order in permutations(lines):
            result = price_basket(order, 100, None)
            self.assertEqual({line.line_id.int: line.net_fcfa for line in result.lines}, {1: 966, 2: 967, 3: 967})

    def test_many_baskets_preserve_every_franc_and_nonnegative_lines(self):
        for first in range(0, 12):
            for second in range(0, 12):
                for discount in range(first + second + 1):
                    result = price_basket([self.line(1, first), self.line(2, second)], discount, None)
                    self.assertIsInstance(result, PricedBasket)
                    self.assertEqual(sum(line.global_discount_fcfa for line in result.lines), discount)
                    self.assertEqual(sum(line.net_fcfa for line in result.lines), first + second - discount)
                    self.assertTrue(all(line.net_fcfa >= 0 for line in result.lines))

    def test_results_are_immutable(self):
        result = price_basket([self.line(1, 1000)])
        with self.assertRaises(FrozenInstanceError):
            result.lines[0].net_fcfa = 0

    def test_fractional_line_rounds_half_up_once_before_discount(self):
        for quantity, price, expected in [(500, 249, 125), (1250, 999, 1249), (1500, 1, 2), (1, 500, 1), (1, 499, 0)]:
            with self.subTest(quantity=quantity, price=price):
                result = line_reference_amount(quantity, price, "weight")
                self.assertIsInstance(result, ReferenceAmount)
                self.assertEqual(result.amount_fcfa, expected)

    def test_quantity_and_price_validation_without_binary_floats(self):
        for quantity, price, kind, code in [(1500, 1000, "piece", "fractional_piece"),
                                           (0, 1000, "weight", "invalid_quantity"),
                                           (1000.5, 1000, "volume", "invalid_quantity"),
                                           (1000, True, "piece", "invalid_unit_price"),
                                           (1000, -1, "piece", "invalid_unit_price"),
                                           (1000, 1000, "other", "invalid_unit_kind"),
                                           (2000, 9223372036854775807, "piece", "amount_out_of_range")]:
            with self.subTest(code=code):
                result = line_reference_amount(quantity, price, kind)
                self.assertIsInstance(result, PricingFailure)
                self.assertEqual(result.code, code)
        self.assertEqual(line_reference_amount(2000, 4800, "piece").amount_fcfa, 9600)
