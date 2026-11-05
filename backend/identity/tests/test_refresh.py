from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from identity.models import DeviceSession, Installation, Membership, Shop, ShopDeviceAccess, User


class RefreshTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="seller")
        self.owner = User.objects.create_user(username="owner")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        self.other_shop = Shop.objects.create(name="B", owner=self.owner)
        Membership.objects.create(shop=self.shop, user=self.user)
        Membership.objects.create(shop=self.other_shop, user=self.user)
        self.installation = Installation.objects.create(user=self.user)
        self.session = DeviceSession.objects.create(
            installation=self.installation, expires_at=timezone.now() + timedelta(days=1),
        )
        self.refresh = RefreshToken.for_user(self.user)
        self.refresh["session_id"] = str(self.session.id)
        self.session.refresh_jti = self.refresh["jti"]
        self.session.save()
        self.client = APIClient()

    def renew(self, token=None):
        return self.client.post("/api/v1/auth/refresh/", {"refresh": str(token or self.refresh)}, format="json")

    def test_rotation_issues_five_minute_access_and_new_thirty_day_refresh(self):
        response = self.renew()
        self.assertEqual(response.status_code, 200)
        access = AccessToken(response.data["access"])
        refresh = RefreshToken(response.data["refresh"])
        self.assertEqual(access["exp"] - access["iat"], 300)
        self.assertEqual(refresh["exp"] - refresh["iat"], 2592000)
        self.assertEqual(access["session_id"], str(self.session.id))
        self.assertEqual(refresh["session_id"], str(self.session.id))
        self.assertNotEqual(refresh["jti"], self.refresh["jti"])
        self.session.refresh_from_db()
        self.assertEqual(self.session.refresh_jti, refresh["jti"])
        self.assertEqual(int(self.session.expires_at.timestamp()), refresh["exp"])
        self.assertEqual(self.renew().status_code, 401)
        self.assertEqual(self.renew(refresh).status_code, 200)

    def test_refresh_does_not_restore_revoked_shop_or_renew_offline_lease(self):
        expired = timezone.now() - timedelta(days=1)
        grant = ShopDeviceAccess.objects.create(
            shop=self.shop, installation=self.installation, revoked_at=timezone.now(),
            verified_at=expired - timedelta(days=7), offline_expires_at=expired,
        )
        response = self.renew()
        self.assertEqual(response.status_code, 200)
        grant.refresh_from_db()
        self.assertIsNotNone(grant.revoked_at)
        self.assertEqual(grant.offline_expires_at, expired)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.shop.id}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.other_shop.id}/").status_code, 200)

    def test_revoked_session_or_installation_cannot_refresh(self):
        self.session.revoked_at = timezone.now()
        self.session.save()
        self.assertEqual(self.renew().status_code, 401)
        self.session.revoked_at = None
        self.session.save()
        self.installation.revoked_at = timezone.now()
        self.installation.save()
        self.assertEqual(self.renew().status_code, 401)

    def test_expired_session_cannot_be_resurrected_by_longer_lived_refresh(self):
        self.session.expires_at = timezone.now() - timedelta(seconds=1)
        self.session.save()
        self.assertEqual(self.renew().status_code, 401)

    def test_disabled_account_is_refused_without_consuming_its_refresh(self):
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.renew().status_code, 401)
        self.session.refresh_from_db()
        self.assertEqual(self.session.refresh_jti, self.refresh["jti"])

    def test_missing_malformed_or_foreign_session_is_refused(self):
        bare = RefreshToken.for_user(self.user)
        self.assertEqual(self.renew(bare).status_code, 401)
        bare["session_id"] = "bad-uuid"
        self.assertEqual(self.renew(bare).status_code, 401)
        bare["session_id"] = str(uuid.uuid4())
        self.assertEqual(self.renew(bare).status_code, 401)
        foreign = RefreshToken.for_user(self.owner)
        foreign["session_id"] = str(self.session.id)
        self.assertEqual(self.renew(foreign).status_code, 401)

    def test_stale_refresh_identity_cannot_rotate_another_token_chain(self):
        self.session.refresh_jti = uuid.uuid4().hex
        self.session.save()
        self.assertEqual(self.renew().status_code, 401)

    def test_access_token_or_missing_payload_cannot_refresh(self):
        self.assertEqual(self.renew(self.refresh.access_token).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/auth/refresh/", {}, format="json").status_code, 400)
