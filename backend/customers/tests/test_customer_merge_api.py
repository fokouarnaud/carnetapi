"""US-20 / G18: POST /clients/{source}/merge/ — manager only, idempotent by operation UUID."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from customers.models import Customer
from identity.models import DeviceSession, Installation, Membership, Shop, User


class CustomerMergeApiTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="merge-api-owner")
        self.seller = User.objects.create_user(username="merge-api-seller")
        self.shop = Shop.objects.create(owner=self.owner, name="A")
        Membership.objects.create(shop=self.shop, user=self.seller)
        self.kept = Customer.objects.create(shop=self.shop, name="Awa")
        self.duplicate = Customer.objects.create(shop=self.shop, name="Awa T.")
        self.client = APIClient()

    def login(self, user):
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def merge(self, **changes):
        data = {"operation_id": str(uuid.uuid4()), "target_id": str(self.kept.id), "reason": "Même cliente", **changes}
        return self.client.post(f"/api/v1/shops/{self.shop.id}/clients/{self.duplicate.id}/merge/", data, format="json"), data

    def test_manager_merges_and_replay_returns_the_same_receipt(self):
        self.login(self.owner)
        response, data = self.merge()
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["merged_into"], str(self.kept.id))
        replay = self.client.post(f"/api/v1/shops/{self.shop.id}/clients/{self.duplicate.id}/merge/", data, format="json")
        self.assertEqual((replay.status_code, replay.data), (200, response.data))

    def test_seller_and_blank_reason_are_refused_without_effect(self):
        self.login(self.seller)
        self.assertEqual(self.merge()[0].status_code, 403)
        self.login(self.owner)
        self.assertEqual(self.merge(reason=" ")[0].status_code, 400)
        self.duplicate.refresh_from_db()
        self.assertIsNone(self.duplicate.merged_into_id)
