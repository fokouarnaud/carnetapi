"""US-07 / G22: recovery case endpoints are reserved to the technical superadministrator."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from identity.models import AccountRecoveryCase, DeviceSession, Installation, Membership, Shop, User


class AccountRecoveryApiTests(TestCase):
    def setUp(self):
        self.account = User.objects.create_user(username="api-lost-owner", phone="+237690000011")
        self.member = User.objects.create_user(username="api-loyal-seller")
        self.admin = User.objects.create_superuser(username="api-recovery-admin", password="x")
        shop = Shop.objects.create(name="A", owner=self.account)
        Membership.objects.create(shop=shop, user=self.member)
        self.client = APIClient()

    def login(self, user):
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def open(self, **changes):
        data = {"account_id": str(self.account.id), "new_phone": "+237690000012", "identity_reference": "CNI-77",
                "facts_checked": 3, "confirming_member_id": str(self.member.id), "reason": "Téléphone et codes perdus", **changes}
        return self.client.post("/api/v1/technical/recovery-cases/", data, format="json")

    def test_superadmin_opens_a_case_but_cannot_complete_it_before_seventy_two_hours(self):
        self.login(self.admin)
        response = self.open()
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["state"], "waiting")
        completed = self.client.post(f"/api/v1/technical/recovery-cases/{response.data['id']}/complete/", {}, format="json")
        self.assertEqual(completed.status_code, 400)
        self.assertEqual(AccountRecoveryCase.objects.count(), 1)

    def test_business_roles_and_missing_proofs_are_refused(self):
        self.login(self.member)
        self.assertEqual(self.open().status_code, 403)
        self.login(self.admin)
        self.assertEqual(self.open(facts_checked=2).status_code, 400)
