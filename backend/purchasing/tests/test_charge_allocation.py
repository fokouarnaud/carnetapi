"""US-34: deterministic fee allocation preserves totals and refuses implicit free-line weights."""
import uuid
from django.test import SimpleTestCase


class ChargeAllocationTests(SimpleTestCase):
    def allocate(self, amount, values):
        from purchasing.services.charge_allocation import allocate_charge, ChargeWeight
        return allocate_charge(amount, [ChargeWeight(uuid.UUID(int=index), value) for index, value in values])

    def test_4000_on_10000_and_30000_allocates_1000_and_3000(self):
        result = self.allocate(4000, [(1, 10000), (2, 30000)])
        self.assertEqual([(row.line_id.int, row.amount_fcfa) for row in result.allocations], [(1, 1000), (2, 3000)])

    def test_largest_remainder_ties_use_uuid_and_input_reordering_keeps_the_same_amounts(self):
        result = self.allocate(2, [(3, 1), (1, 1), (2, 1)])
        self.assertEqual({row.line_id.int: row.amount_fcfa for row in result.allocations}, {1: 1, 2: 1, 3: 0})
        reordered = self.allocate(2, [(2, 1), (3, 1), (1, 1)])
        self.assertEqual({row.line_id.int: row.amount_fcfa for row in reordered.allocations}, {1: 1, 2: 1, 3: 0})

    def test_zero_or_unknown_line_requires_explicit_allocation_even_when_other_weights_are_known(self):
        for value in (0, None):
            with self.subTest(value=value):
                result = self.allocate(4000, [(1, value), (2, 30000)])
                self.assertEqual(result.code, "explicit_charge_allocation_required")
                self.assertEqual(result.line_ids, (uuid.UUID(int=1),))

    def test_large_amounts_and_zero_fee_are_exact_without_float_intermediates(self):
        result = self.allocate(9223372036854775807, [(1, 1), (2, 1)])
        self.assertEqual([row.amount_fcfa for row in result.allocations], [4611686018427387904, 4611686018427387903])
        self.assertEqual([row.amount_fcfa for row in self.allocate(0, [(1, 1), (2, 3)]).allocations], [0, 0])

    def test_invalid_amount_duplicate_empty_or_invalid_weights_are_refused(self):
        for amount in (-1, 0.5, True, 9223372036854775808):
            with self.subTest(amount=amount):
                self.assertEqual(self.allocate(amount, [(1, 1)]).code, "invalid_charge_amount")
        self.assertEqual(self.allocate(1000, []).code, "charge_lines_required")
        self.assertEqual(self.allocate(1000, [(1, 1), (1, 2)]).code, "duplicate_charge_line")
        for value in (-1, 1.5, True):
            with self.subTest(value=value):
                self.assertEqual(self.allocate(1000, [(1, value)]).code, "invalid_charge_value")
