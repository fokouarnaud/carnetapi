"""US-05 / US-17 / Q136, decision of 13 November 2026: an offline sale keeps its device-recorded
time when it falls inside the offline lease length (7 days before reception, not in the future
beyond a 5-minute clock tolerance). Distinct from manual backdating (G15)."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from cash.models import CashMovement
from sales.models import Sale
from sales.tests import test_sales as base
from stock.models import StockMovement


class OfflineRecordedAtTests(TestCase):
    setUp = base.CashSaleTests.setUp
    authenticate = base.CashSaleTests.authenticate
    url = base.CashSaleTests.url
    payload = base.CashSaleTests.payload
    create = base.CashSaleTests.create

    def iso(self, moment):
        return moment.isoformat().replace("+00:00", "Z")

    def test_seller_sale_keeps_device_time_and_reception_time_separately(self):
        recorded = timezone.now() - timedelta(days=3)
        data = self.payload(recorded_at=self.iso(recorded))
        response = self.create(data)
        self.assertEqual(response.status_code, 201, response.data)
        sale = Sale.objects.get(id=data["id"])
        self.assertEqual(sale.occurred_at, recorded)
        self.assertGreater(sale.created_at, recorded + timedelta(days=2))
        movements = StockMovement.objects.filter(kind="sale", product=self.product)
        self.assertEqual({row.occurred_at for row in movements}, {recorded})
        self.assertTrue(CashMovement.objects.filter(occurred_at=recorded).exists())

    def test_device_time_older_than_the_lease_is_refused_without_effect(self):
        data = self.payload(recorded_at=self.iso(timezone.now() - timedelta(days=7, minutes=1)))
        response = self.create(data)
        self.assertEqual(response.status_code, 400)
        self.assertIn("recorded_at", response.data)
        self.assertFalse(Sale.objects.filter(id=data["id"]).exists())

    def test_small_clock_skew_is_tolerated_but_a_future_time_is_refused(self):
        skewed = self.payload(recorded_at=self.iso(timezone.now() + timedelta(minutes=3)))
        self.assertEqual(self.create(skewed).status_code, 201)
        future = self.payload(recorded_at=self.iso(timezone.now() + timedelta(minutes=10)))
        self.assertEqual(self.create(future).status_code, 400)

    def test_recorded_time_and_manual_backdating_are_mutually_exclusive(self):
        data = self.payload(recorded_at=self.iso(timezone.now() - timedelta(hours=1)),
                            occurred_at=self.iso(timezone.now() - timedelta(days=1)), reason="Oubli")
        self.assertEqual(self.create(data).status_code, 400)

    def test_replay_returns_the_original_receipt_once(self):
        data = self.payload(recorded_at=self.iso(timezone.now() - timedelta(days=1)))
        first = self.create(data)
        second = self.create(data)
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.data, second.data)
        self.assertEqual(Sale.objects.filter(id=data["id"]).count(), 1)

    def test_without_device_time_the_sale_is_dated_at_reception(self):
        before = timezone.now()
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        self.assertGreaterEqual(Sale.objects.get(id=data["id"]).occurred_at, before)
