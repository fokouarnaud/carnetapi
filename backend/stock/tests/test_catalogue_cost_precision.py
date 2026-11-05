"""Catalogue idempotency must distinguish exact opening costs beyond float precision."""
from django.test import TestCase
from common.models import Activity
from stock.models import StockMovement
from stock.tests import test_stock_thresholds as threshold_tests


class CatalogueCostPrecisionTests(TestCase):
    setUp = threshold_tests.StockThresholdApiTests.setUp
    authenticate = threshold_tests.StockThresholdApiTests.authenticate
    payload = threshold_tests.StockThresholdApiTests.payload
    url = threshold_tests.StockThresholdApiTests.url

    def test_adjacent_large_costs_are_different_requests_even_for_legacy_float_audits(self):
        body = self.payload(opening={"quantity_milli": 1000, "unit_cost_fcfa": "9223372036854.775806",
                            "occurred_at": "2026-01-01T10:00:00Z", "reason": "Stock réel"})
        created = self.client.post(self.url(), body, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(StockMovement.objects.get().unit_cost_micros, 9223372036854775806)
        different = {**body, "opening": {**body["opening"], "unit_cost_fcfa": "9223372036854.775807"}}
        self.assertEqual(self.client.post(self.url(), different, format="json").status_code, 409)
        event = Activity.objects.get(id=body["operation_id"])
        event.details["request"]["opening"]["unit_cost_fcfa"] = float(body["opening"]["unit_cost_fcfa"])
        event.save(update_fields=["details"])
        self.assertEqual(self.client.post(self.url(), body, format="json").data, created.data)
        self.assertEqual(self.client.post(self.url(), different, format="json").status_code, 409)
        self.assertEqual(StockMovement.objects.count(), 1)
