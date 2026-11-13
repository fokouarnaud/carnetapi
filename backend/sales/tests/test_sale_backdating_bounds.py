"""US-35 / G15: a dated sale is limited to 30 days; beyond, owner only with a second reason."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from identity.models import Membership
from sales.models import Sale
from sales.tests import test_sales as sale_tests


class SaleBackdatingBoundTests(TestCase):
    setUp = sale_tests.CashSaleTests.setUp
    authenticate = sale_tests.CashSaleTests.authenticate
    payload = sale_tests.CashSaleTests.payload
    url = sale_tests.CashSaleTests.url

    def dated(self, days_ago, **changes):
        occurred = (timezone.now() - timedelta(days=days_ago)).isoformat()
        return self.client.post(self.url(), self.payload(occurred_at=occurred, reason="Vente oubliée", **changes), format="json")

    def test_member_manager_is_limited_to_thirty_days(self):
        self.member.role = Membership.Role.MANAGER
        self.member.save(update_fields=["role"])
        self.assertEqual(self.dated(10).status_code, 201)
        self.assertEqual(self.dated(31).status_code, 400)
        self.assertEqual(self.dated(31, backdate_reason="Carnet retrouvé").status_code, 400)
        self.assertEqual(Sale.objects.count(), 1)

    def test_owner_beyond_thirty_days_needs_a_second_reason(self):
        self.authenticate(self.owner)
        self.assertEqual(self.dated(40).status_code, 400)
        self.assertEqual(self.dated(40, backdate_reason="Carnet de ventes retrouvé").status_code, 201)
