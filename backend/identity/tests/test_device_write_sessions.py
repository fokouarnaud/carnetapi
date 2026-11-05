"""A withdrawal between authentication and a device command cannot grant rights."""
from datetime import timedelta
import uuid
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from identity.api.serializers import RevokeDeviceSerializer
from identity.api.views import ShopViewSet
from identity.models import DeviceSession, Installation, Membership, Shop, ShopDeviceAccess, User


class DeviceWriteSessionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="device-write-owner")
        self.seller = User.objects.create_user(username="device-write-seller")
        self.shop = Shop.objects.create(name="Device A", owner=self.owner)
        self.other_shop = Shop.objects.create(name="Device B", owner=self.owner)
        Membership.objects.create(shop=self.shop, user=self.seller)
        Membership.objects.create(shop=self.other_shop, user=self.seller)
        self.phone = Installation.objects.create(user=self.owner)
        self.target = Installation.objects.create(user=self.seller)
        self.second_target_phone = Installation.objects.create(user=self.seller)
        self.session = DeviceSession.objects.create(
            installation=self.phone, expires_at=timezone.now() + timedelta(days=30))
        DeviceSession.objects.create(installation=self.phone, expires_at=timezone.now() + timedelta(days=30))
        self.grant = ShopDeviceAccess.objects.create(shop=self.shop, installation=self.phone,
            verified_at=timezone.now() - timedelta(days=2), offline_expires_at=timezone.now() + timedelta(days=5))
        ShopDeviceAccess.objects.create(shop=self.other_shop, installation=self.phone,
            verified_at=timezone.now() - timedelta(days=3), offline_expires_at=timezone.now() + timedelta(days=4))
        self.client = APIClient()
        token = AccessToken.for_user(self.owner)
        token["session_id"] = str(self.session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.revoke_body = {"operation_id": str(uuid.uuid4()), "installation_id": str(self.target.id),
                            "reason": "Téléphone perdu"}

    def withdraw(self, state):
        if state == "session_revoked":
            DeviceSession.objects.filter(pk=self.session.pk).update(revoked_at=timezone.now())
        elif state == "installation_revoked":
            Installation.objects.filter(pk=self.phone.pk).update(revoked_at=timezone.now())
        elif state == "user_inactive":
            User.objects.filter(pk=self.owner.pk).update(is_active=False)
        elif state == "shop_revoked":
            ShopDeviceAccess.objects.filter(pk=self.grant.pk).update(revoked_at=timezone.now())

    def revoke(self):
        return self.client.post(f"/api/v1/shops/{self.shop.id}/revoke-device/", self.revoke_body, format="json")

    def assert_revoke_blocked(self, state, retry=False):
        if retry:
            self.assertEqual(self.revoke().status_code, 200)
        before_grants = list(ShopDeviceAccess.objects.exclude(pk=self.grant.pk).order_by("id").values())
        before_history = list(Activity.objects.order_by("id").values())
        original = RevokeDeviceSerializer.run_validation

        def validate(serializer, data):
            result = original(serializer, data)
            self.withdraw(state)
            return result

        with patch.object(RevokeDeviceSerializer, "run_validation", autospec=True, side_effect=validate):
            response = self.revoke()
        self.assertEqual(response.status_code, 404 if state == "shop_revoked" else 401)
        self.assertNotIn("installation_id", response.data)
        self.assertEqual(list(ShopDeviceAccess.objects.exclude(pk=self.grant.pk).order_by("id").values()), before_grants)
        self.assertEqual(list(Activity.objects.order_by("id").values()), before_history)

    def assert_lease_blocked(self, state):
        before = ShopDeviceAccess.objects.values().get(pk=self.grant.pk)
        before_other = list(ShopDeviceAccess.objects.exclude(pk=self.grant.pk).order_by("id").values())
        original = ShopViewSet.get_object

        def get_object(view):
            result = original(view)
            self.withdraw(state)
            return result

        with patch.object(ShopViewSet, "get_object", autospec=True, side_effect=get_object):
            response = self.client.post(f"/api/v1/shops/{self.shop.id}/verify-access/", {}, format="json")
        self.assertEqual(response.status_code, 404 if state == "shop_revoked" else 401)
        self.assertNotIn("offline_expires_at", response.data)
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.verified_at, before["verified_at"])
        self.assertEqual(self.grant.offline_expires_at, before["offline_expires_at"])
        self.assertEqual(self.grant.updated_at, before["updated_at"])
        self.assertEqual(list(ShopDeviceAccess.objects.exclude(pk=self.grant.pk).order_by("id").values()), before_other)
        self.assertFalse(Activity.objects.exists())

    def test_revoked_session_cannot_extend_lease(self):
        self.assert_lease_blocked("session_revoked")

    def test_revoked_installation_cannot_extend_lease(self):
        self.assert_lease_blocked("installation_revoked")

    def test_disabled_user_cannot_extend_lease(self):
        self.assert_lease_blocked("user_inactive")

    def test_current_shop_withdrawal_cannot_extend_lease(self):
        self.assert_lease_blocked("shop_revoked")

    def test_revoked_session_cannot_revoke_a_phone(self):
        self.assert_revoke_blocked("session_revoked")

    def test_revoked_installation_cannot_revoke_a_phone(self):
        self.assert_revoke_blocked("installation_revoked")

    def test_disabled_user_cannot_revoke_a_phone(self):
        self.assert_revoke_blocked("user_inactive")

    def test_current_shop_withdrawal_cannot_revoke_a_phone(self):
        self.assert_revoke_blocked("shop_revoked")

    def test_revoked_session_cannot_read_an_existing_revoke_receipt(self):
        self.assert_revoke_blocked("session_revoked", retry=True)

    def test_revoked_installation_cannot_read_an_existing_revoke_receipt(self):
        self.assert_revoke_blocked("installation_revoked", retry=True)

    def test_disabled_user_cannot_read_an_existing_revoke_receipt(self):
        self.assert_revoke_blocked("user_inactive", retry=True)

    def test_current_shop_withdrawal_cannot_read_an_existing_revoke_receipt(self):
        self.assert_revoke_blocked("shop_revoked", retry=True)

    def test_live_revocation_keeps_other_shop_and_other_phone_available(self):
        first = self.revoke()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.revoke().data, first.data)
        self.assertEqual(Activity.objects.count(), 1)
        self.assertIsNotNone(ShopDeviceAccess.objects.get(shop=self.shop, installation=self.target).revoked_at)
        self.assertFalse(ShopDeviceAccess.objects.filter(shop=self.other_shop, installation=self.target).exists())
        self.assertFalse(ShopDeviceAccess.objects.filter(installation=self.second_target_phone).exists())
