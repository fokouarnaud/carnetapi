from datetime import datetime, timedelta, timezone as dt_timezone
import uuid
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from identity.models import DeviceSession, Installation, Membership, Shop, ShopDeviceAccess, User


class DeviceAccessTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner")
        self.seller = User.objects.create_user(username="seller")
        self.manager = User.objects.create_user(username="manager")
        self.a = Shop.objects.create(name="A", owner=self.owner)
        self.b = Shop.objects.create(name="B", owner=self.owner)
        Membership.objects.create(shop=self.a, user=self.seller)
        Membership.objects.create(shop=self.b, user=self.seller)
        Membership.objects.create(shop=self.a, user=self.manager, role=Membership.Role.MANAGER)
        self.phone = Installation.objects.create(user=self.seller)
        self.second_phone = Installation.objects.create(user=self.seller)
        self.session = DeviceSession.objects.create(installation=self.phone, expires_at=timezone.now() + timedelta(days=30))
        self.client = APIClient()

    def authenticate(self, user, session=None):
        if session is None:
            device = Installation.objects.create(user=user)
            session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return session

    def revoke(self, shop=None, installation=None, reason="Téléphone perdu", operation_id=None):
        return self.client.post(f"/api/v1/shops/{(shop or self.a).id}/revoke-device/", {
            "installation_id": str((installation or self.phone).id),
            "reason": reason,
            "operation_id": str(operation_id or uuid.uuid4()),
        }, format="json")

    def test_bare_token_cannot_bypass_installation_checks(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(self.seller)}")
        self.assertEqual(self.client.get("/api/v1/shops/").status_code, 401)

    def test_token_cannot_claim_another_accounts_session(self):
        self.authenticate(self.owner, self.session)
        self.assertEqual(self.client.get("/api/v1/shops/").status_code, 401)

    def test_revoked_session_and_revoked_installation_reject_existing_jwt(self):
        self.authenticate(self.seller, self.session)
        self.session.revoked_at = timezone.now()
        self.session.save()
        self.assertEqual(self.client.get("/api/v1/shops/").status_code, 401)
        self.session.revoked_at = None
        self.session.save()
        self.phone.revoked_at = timezone.now()
        self.phone.save()
        self.assertEqual(self.client.get("/api/v1/shops/").status_code, 401)

    def test_expired_server_session_blocks_a_still_valid_jwt(self):
        self.authenticate(self.seller, self.session)
        self.session.expires_at = timezone.now() - timedelta(seconds=1)
        self.session.save()
        self.assertEqual(self.client.get("/api/v1/shops/").status_code, 401)

    def test_lease_is_seven_days_from_server_verification_and_shop_specific(self):
        self.authenticate(self.seller, self.session)
        now = datetime(2026, 10, 11, 12, 0, tzinfo=dt_timezone.utc)
        with patch("django.utils.timezone.now", return_value=now):
            response = self.client.post(f"/api/v1/shops/{self.a.id}/verify-access/", {}, format="json")
        self.assertEqual(response.status_code, 200)
        grant = ShopDeviceAccess.objects.get(shop=self.a, installation=self.phone)
        self.assertEqual(grant.verified_at, now)
        self.assertEqual(grant.offline_expires_at, datetime(2026, 10, 18, 12, 0, tzinfo=dt_timezone.utc))
        self.assertFalse(ShopDeviceAccess.objects.filter(shop=self.b, installation=self.phone).exists())
        self.assertEqual(response.data["permissions"]["can_grant_credit"], False)
        self.assertEqual(response.data["permissions"]["can_collect_debt"], True)
        self.assertEqual(response.data["permissions"]["can_read_debt"], True)

    def test_revoke_one_shop_keeps_other_shop_and_other_phone(self):
        self.authenticate(self.manager)
        response = self.revoke()
        self.assertEqual(response.status_code, 200)
        self.authenticate(self.seller, self.session)
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.a.id}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.b.id}/").status_code, 200)
        second = DeviceSession.objects.create(installation=self.second_phone, expires_at=timezone.now() + timedelta(days=30))
        self.authenticate(self.seller, second)
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.a.id}/").status_code, 200)

    def test_revoked_device_cannot_renew_lease(self):
        ShopDeviceAccess.objects.create(shop=self.a, installation=self.phone, revoked_at=timezone.now())
        self.authenticate(self.seller, self.session)
        response = self.client.post(f"/api/v1/shops/{self.a.id}/verify-access/", {}, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertIsNone(ShopDeviceAccess.objects.get(shop=self.a, installation=self.phone).verified_at)

    def test_revoke_requires_reason_and_writes_audit_once_on_retry(self):
        self.authenticate(self.owner)
        self.assertEqual(self.revoke(reason="   ").status_code, 400)
        self.assertFalse(ShopDeviceAccess.objects.filter(shop=self.a, installation=self.phone).exists())
        operation_id = uuid.uuid4()
        self.assertEqual(self.revoke(operation_id=operation_id).status_code, 200)
        self.assertEqual(self.revoke(operation_id=operation_id).status_code, 200)
        audit = Activity.objects.get(id=operation_id)
        self.assertEqual(audit.actor_id, self.owner.id)
        self.assertEqual(audit.reason, "Téléphone perdu")
        self.assertEqual(Activity.objects.count(), 1)
        self.assertEqual(self.revoke(operation_id=operation_id, reason="Autre motif").status_code, 409)

    def test_seller_cannot_revoke_and_manager_cannot_revoke_other_manager(self):
        self.authenticate(self.seller, self.session)
        self.assertEqual(self.revoke().status_code, 403)
        another_manager = User.objects.create_user(username="manager2")
        Membership.objects.create(shop=self.a, user=another_manager, role=Membership.Role.MANAGER)
        target = Installation.objects.create(user=another_manager)
        self.authenticate(self.manager)
        self.assertEqual(self.revoke(installation=target).status_code, 403)
        self.authenticate(self.owner)
        self.assertEqual(self.revoke(installation=target).status_code, 200)

    def test_foreign_shop_and_foreign_installation_cannot_be_mutated(self):
        outsider = User.objects.create_user(username="outsider")
        foreign_shop = Shop.objects.create(name="Other", owner=outsider)
        foreign_phone = Installation.objects.create(user=outsider)
        self.authenticate(self.manager)
        self.assertEqual(self.revoke(shop=foreign_shop).status_code, 404)
        self.assertEqual(self.revoke(installation=foreign_phone).status_code, 404)
        self.assertFalse(Activity.objects.exists())

    def test_verifying_b_does_not_extend_a_and_current_permissions_are_returned(self):
        self.authenticate(self.seller, self.session)
        first = datetime(2026, 10, 11, 12, 0, tzinfo=dt_timezone.utc)
        second = datetime(2026, 10, 13, 12, 0, tzinfo=dt_timezone.utc)
        with patch("django.utils.timezone.now", return_value=first):
            self.assertEqual(self.client.post(f"/api/v1/shops/{self.a.id}/verify-access/", {}, format="json").status_code, 200)
        member = Membership.objects.get(shop=self.b, user=self.seller)
        member.can_collect_debt = False
        member.can_grant_credit = True
        member.save()
        with patch("django.utils.timezone.now", return_value=second):
            response = self.client.post(f"/api/v1/shops/{self.b.id}/verify-access/", {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["permissions"]["can_collect_debt"])
        self.assertTrue(response.data["permissions"]["can_read_debt"])
        self.assertEqual(ShopDeviceAccess.objects.get(shop=self.a, installation=self.phone).offline_expires_at, datetime(2026, 10, 18, 12, 0, tzinfo=dt_timezone.utc))
        self.assertEqual(ShopDeviceAccess.objects.get(shop=self.b, installation=self.phone).offline_expires_at, datetime(2026, 10, 20, 12, 0, tzinfo=dt_timezone.utc))

    def test_retry_returns_original_audit_receipt_even_after_grant_changes(self):
        self.authenticate(self.owner)
        operation = uuid.uuid4()
        first = self.revoke(operation_id=operation)
        self.assertEqual(first.status_code, 200)
        grant = ShopDeviceAccess.objects.get(shop=self.a, installation=self.phone)
        grant.revoked_at = None
        grant.save()
        retry = self.revoke(operation_id=operation)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, first.data)
        grant.refresh_from_db()
        self.assertIsNone(grant.revoked_at)
        self.assertEqual(Activity.objects.count(), 1)
