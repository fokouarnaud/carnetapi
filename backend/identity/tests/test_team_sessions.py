"""Team writes and their receipts require the exact session at commit time."""
from datetime import timedelta
import uuid
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from identity.api.team import ChangeMembershipSerializer
from identity.models import DeviceSession, Installation, Membership, Shop, User


class TeamSessionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="team-session-owner")
        self.seller = User.objects.create_user(username="team-session-seller")
        self.shop = Shop.objects.create(name="Team A", owner=self.owner)
        self.other_shop = Shop.objects.create(name="Team B", owner=self.owner)
        self.member = Membership.objects.create(shop=self.shop, user=self.seller)
        Membership.objects.create(shop=self.other_shop, user=self.seller)
        self.installation = Installation.objects.create(user=self.owner)
        self.session = self.new_session()
        # A different live session cannot replace the one in this request's JWT.
        self.spare_session = self.new_session()
        token = AccessToken.for_user(self.owner)
        token["session_id"] = str(self.session.id)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.url = f"/api/v1/shops/{self.shop.id}/team/{self.member.id}/"
        self.payload = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                        "reason": "Nomination confirmée", "role": "manager"}

    def new_session(self):
        return DeviceSession.objects.create(
            installation=self.installation, expires_at=timezone.now() + timedelta(days=30))

    def change(self):
        return self.client.patch(self.url, self.payload, format="json")

    def revoke_during_validation(self, state):
        original = ChangeMembershipSerializer.run_validation

        def validate(serializer, data):
            validated = original(serializer, data)
            if state == "session_revoked":
                DeviceSession.objects.filter(pk=self.session.pk).update(revoked_at=timezone.now())
            elif state == "installation_revoked":
                Installation.objects.filter(pk=self.installation.pk).update(revoked_at=timezone.now())
            elif state == "user_inactive":
                User.objects.filter(pk=self.owner.pk).update(is_active=False)
            return validated

        return patch.object(ChangeMembershipSerializer, "run_validation", autospec=True, side_effect=validate)

    def assert_blocked_after_validation(self, state, retry=False):
        if retry:
            self.assertEqual(self.change().status_code, 200)
        before_memberships = list(Membership.objects.order_by("id").values())
        before_history = list(Activity.objects.order_by("id").values())
        with self.revoke_during_validation(state):
            response = self.change()
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("user_id", response.data)
        self.assertEqual(list(Membership.objects.order_by("id").values()), before_memberships)
        self.assertEqual(list(Activity.objects.order_by("id").values()), before_history)

    def test_new_promotion_refused_if_exact_session_is_revoked_during_validation(self):
        self.assert_blocked_after_validation("session_revoked")

    def test_new_promotion_refused_if_installation_is_revoked_during_validation(self):
        self.assert_blocked_after_validation("installation_revoked")

    def test_new_promotion_refused_if_user_is_disabled_during_validation(self):
        self.assert_blocked_after_validation("user_inactive")

    def test_existing_receipt_refused_if_exact_session_is_revoked_during_validation(self):
        self.assert_blocked_after_validation("session_revoked", retry=True)

    def test_existing_receipt_refused_if_installation_is_revoked_during_validation(self):
        self.assert_blocked_after_validation("installation_revoked", retry=True)

    def test_existing_receipt_refused_if_user_is_disabled_during_validation(self):
        self.assert_blocked_after_validation("user_inactive", retry=True)

    def test_live_session_promotes_and_replays_without_a_second_event(self):
        response = self.change()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], "manager")
        self.assertEqual(response.data["version"], 2)
        replay = self.change()
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.data, response.data)
        self.assertEqual(Activity.objects.count(), 1)
        other = Membership.objects.get(shop=self.other_shop, user=self.seller)
        self.assertEqual(other.role, "seller")
        self.assertEqual(other.version, 1)

    def test_revocation_of_a_different_session_keeps_current_session_authorized(self):
        DeviceSession.objects.filter(pk=self.spare_session.pk).update(revoked_at=timezone.now())
        self.assertEqual(self.change().status_code, 200)
        self.assertEqual(Activity.objects.count(), 1)
