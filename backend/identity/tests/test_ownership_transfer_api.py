"""US-47 / G24: POST /technical/shops/{id}/ownership-transfer/ — superadmin only, reason and proofs required."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from identity.models import DeviceSession, Installation, Shop, User


class OwnershipTransferApiTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="api-old-owner")
        self.buyer = User.objects.create_user(username="api-new-owner")
        self.admin = User.objects.create_superuser(username="api-tech", password="x")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        self.client = APIClient()

    def login(self, user):
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def transfer(self, **changes):
        data = {"new_owner_id": str(self.buyer.id), "reason": "Vente du fonds",
                "proofs": {"identity_reference": "CNI-9", "facts_checked": 3, "confirming_member": "vendeur"}, **changes}
        return self.client.post(f"/api/v1/technical/shops/{self.shop.id}/ownership-transfer/", data, format="json")

    def test_superadmin_transfers_and_owner_cannot(self):
        self.login(self.owner)
        self.assertEqual(self.transfer().status_code, 403)
        self.login(self.admin)
        response = self.transfer()
        self.assertEqual(response.status_code, 201, response.content)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.owner, self.buyer)

    def test_missing_proofs_are_refused(self):
        self.login(self.admin)
        self.assertEqual(self.transfer(proofs={}).status_code, 400)
