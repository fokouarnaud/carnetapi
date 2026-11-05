"""DOD-08 separates the strict initial budget from twelve-month capacity."""
from django.test import SimpleTestCase


class StorageAssessmentTests(SimpleTestCase):
    def test_initial_budget_is_strict_and_missing_or_partial_inventory_is_not_a_pass(self):
        from verification.storage_model import initial_budget_status
        self.assertEqual(initial_budget_status(499999999, inventory_complete=True), "within_limit")
        self.assertEqual(initial_budget_status(500000000, inventory_complete=True), "exceeds_limit")
        self.assertEqual(initial_budget_status(500000001, inventory_complete=False), "exceeds_limit")
        self.assertEqual(initial_budget_status(270000000, inventory_complete=False), "not_demonstrated")
        self.assertEqual(initial_budget_status(None, inventory_complete=False), "not_demonstrated")

    def test_twelve_month_floor_does_not_reuse_the_initial_budget_as_a_host_quota(self):
        from verification.storage_model import annual_floor_status
        self.assertEqual(annual_floor_status(900000000, host_limit_bytes=None), "host_limit_unknown")
        self.assertEqual(annual_floor_status(900000000, host_limit_bytes=500000000), "floor_exceeds_host_limit")
        self.assertEqual(annual_floor_status(100000000, host_limit_bytes=500000000), "capacity_not_demonstrated")
        self.assertEqual(annual_floor_status(500000000, host_limit_bytes=500000000), "capacity_not_demonstrated")

    def test_invalid_measurements_never_turn_into_a_zero_byte_success(self):
        from verification.storage_model import initial_budget_status, annual_floor_status
        for invalid in (-1, True, 1.5, "200"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                initial_budget_status(invalid, inventory_complete=True)
        with self.assertRaises(ValueError):
            annual_floor_status(100, host_limit_bytes=0)

    def test_workload_uses_per_shop_volume_and_retains_the_prd_dimensions(self):
        from verification.storage_model import Workload
        target = Workload()
        self.assertEqual((target.sales, target.lines, target.products, target.devices), (73000, 365000, 2000, 6))
        later = Workload(shops=10)
        self.assertEqual((later.sales, later.lines, later.products, later.devices), (365000, 1825000, 10000, 30))
        with self.assertRaises(ValueError):
            Workload(shops=True)
