"""Approved cumulative half-up rule keeps successive returns equal to net billed."""
from django.test import SimpleTestCase


class ReturnPricingTests(SimpleTestCase):
    def amount(self, net, sold, before, quantity):
        from sales.services.return_pricing import return_amount_fcfa
        return return_amount_fcfa(net, sold, before, quantity)

    def test_three_single_piece_returns_refund_33_then_34_then_33(self):
        self.assertEqual([self.amount(100, 3000, prior, 1000) for prior in (0, 1000, 2000)], [33, 34, 33])
        self.assertEqual(self.amount(100, 3000, 0, 3000), 100)
        self.assertEqual(self.amount(100, 3000, 0, 2000), 67)
        self.assertEqual(self.amount(100, 3000, 2000, 1000), 33)

    def test_fractional_carton_uses_original_net_and_base_quantity(self):
        self.assertEqual(self.amount(4800, 24000, 0, 3000), 600)
        self.assertEqual(self.amount(125, 500, 0, 125), 31)
        self.assertEqual(self.amount(125, 500, 125, 125), 32)

    def test_zero_amount_and_half_up_boundaries(self):
        self.assertEqual(self.amount(0, 3000, 0, 1000), 0)
        self.assertEqual([self.amount(1, 3000, prior, 1000) for prior in (0, 1000, 2000)], [0, 1, 0])
        self.assertEqual(self.amount(1, 2000, 0, 1000), 1)
        self.assertEqual(self.amount(1, 2000, 1000, 1000), 0)

    def test_large_integer_calculations_never_use_binary_floats(self):
        self.assertEqual(self.amount(9223372036854775807, 1000, 0, 500), 4611686018427387904)
        self.assertEqual(self.amount(9223372036854775807, 1000, 500, 500), 4611686018427387903)

    def test_invalid_or_over_returned_quantities_are_refused(self):
        for args in ((-1,1000,0,1000), (1,0,0,1), (1,1000,-1,1), (1,1000,0,0),
                     (1,1000,500,501), (True,1000,0,1), (1,1000,0,1.5)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.amount(*args)
