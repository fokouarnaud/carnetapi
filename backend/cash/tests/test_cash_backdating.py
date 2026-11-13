"""US-35 / G15: manual cash movements are backdated at most 30 days, beyond only by the owner."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from cash.models import CashMovement
from identity.models import DeviceSession, Installation, Membership, Shop, User


class CashBackdatingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="backdate-owner")
        self.manager = User.objects.create_user(username="backdate-manager")
        self.shop = Shop.objects.create(owner=self.owner, name="A")
        Membership.objects.create(shop=self.shop, user=self.manager, role=Membership.Role.MANAGER)
        self.client = APIClient()

    def login(self, user):
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        access = AccessToken.for_user(user)
        access["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def post(self, days_ago, **changes):
        occurred = (timezone.now() - timedelta(days=days_ago)).isoformat()
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "kind": "addition", "method": "cash",
                "amount_fcfa": 1000, "occurred_at": occurred, "reason": "Apport oublié", **changes}
        return self.client.post(f"/api/v1/shops/{self.shop.id}/cash/", data, format="json")

    def test_manager_backdates_within_thirty_days_but_not_beyond(self):
        self.login(self.manager)
        self.assertEqual(self.post(10).status_code, 201)
        self.assertEqual(self.post(31).status_code, 400)
        self.assertEqual(self.post(31, backdate_reason="Cahier retrouvé").status_code, 400)
        self.assertEqual(CashMovement.objects.count(), 1)

    def test_owner_goes_beyond_thirty_days_only_with_a_second_reason(self):
        self.login(self.owner)
        self.assertEqual(self.post(40).status_code, 400)
        response = self.post(40, backdate_reason="Cahier de caisse retrouvé")
        self.assertEqual(response.status_code, 201)

    def test_opening_fund_is_a_takeover_and_not_bounded(self):
        self.login(self.manager)
        self.assertEqual(self.post(200, kind="opening").status_code, 201)


class CashBackdatingBeforeCountTests(CashBackdatingTests):
    """G15: a movement dated before a validated count of the same method must name the count it revises."""

    def count(self, days_ago):
        counted = (timezone.now() - timedelta(days=days_ago)).isoformat()
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "counted_at": counted,
                "physical_fcfa": 0, "method": "cash", "reason": "Comptage"}
        response = self.client.post(f"/api/v1/shops/{self.shop.id}/cash-counts/", data, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return data["id"]

    def test_backdating_before_a_count_requires_naming_that_count(self):
        self.login(self.manager)
        counted = self.count(5)
        self.assertEqual(self.post(10).status_code, 400)
        self.assertEqual(self.post(10, revises_count_id=str(uuid.uuid4())).status_code, 400)
        self.assertEqual(self.post(10, revises_count_id=counted).status_code, 201)

    def test_movement_after_the_count_or_another_method_is_unaffected(self):
        self.login(self.manager)
        self.count(5)
        self.assertEqual(self.post(2).status_code, 201)
        self.assertEqual(self.post(10, method="orange").status_code, 201)
