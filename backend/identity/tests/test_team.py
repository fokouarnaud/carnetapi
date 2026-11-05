"""US-03: rights changes are authorized, audited, versioned and shop scoped."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.contrib import admin
from django.test import RequestFactory
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from identity.models import DeviceSession, Installation, Membership, Shop, User


class TeamTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="team-owner")
        self.manager = User.objects.create_user(username="team-manager")
        self.seller = User.objects.create_user(username="team-seller")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        self.other = Shop.objects.create(name="B", owner=self.owner)
        self.manager_member = Membership.objects.create(shop=self.shop, user=self.manager, role="manager")
        self.member = Membership.objects.create(shop=self.shop, user=self.seller)
        self.other_member = Membership.objects.create(shop=self.other, user=self.seller)
        self.client = APIClient()
        self.authenticate(self.owner)

    def authenticate(self, user):
        device = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def url(self, member=None, shop=None):
        return f"/api/v1/shops/{(shop or self.shop).id}/team/{(member or self.member).id}/"

    def payload(self, **changes):
        return {"operation_id": str(uuid.uuid4()), "base_version": 1,
                "reason": "Décision du propriétaire", **changes}

    def change(self, data, member=None, shop=None):
        return self.client.patch(self.url(member, shop), data, format="json")

    def test_owner_promotes_once_with_before_after_and_stable_receipt(self):
        data = self.payload(role="manager")
        response = self.change(data)
        self.assertEqual(response.status_code, 200)
        self.member.refresh_from_db()
        self.assertEqual(self.member.role, "manager")
        self.assertEqual(response.data["version"], 2)
        audit = Activity.objects.get(id=data["operation_id"])
        self.assertEqual(audit.actor_id, self.owner.id)
        self.assertEqual(audit.reason, data["reason"])
        self.assertEqual(audit.details["before"]["role"], "seller")
        self.assertEqual(audit.details["after"]["role"], "manager")
        retry = self.change(data)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, response.data)
        self.assertEqual(Activity.objects.count(), 1)
        self.assertEqual(self.change({**data, "role": "seller"}).status_code, 409)

    def test_manager_cannot_promote_or_change_manager_and_seller_cannot_change_rights(self):
        self.authenticate(self.manager)
        self.assertEqual(self.change(self.payload(role="manager")).status_code, 403)
        self.assertEqual(self.change(self.payload(is_active=False), self.manager_member).status_code, 403)
        self.authenticate(self.seller)
        self.assertEqual(self.change(self.payload(can_grant_credit=True)).status_code, 403)
        self.assertFalse(Activity.objects.exists())

    def test_manager_changes_seller_permissions_only_in_this_shop(self):
        self.authenticate(self.manager)
        response = self.change(self.payload(can_grant_credit=True, can_read_cash_balance=True))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["permissions"]["can_grant_credit"])
        self.assertTrue(response.data["permissions"]["can_read_debt"])
        self.assertTrue(response.data["permissions"]["can_read_cash_balance"])
        self.other_member.refresh_from_db()
        self.assertFalse(self.other_member.can_grant_credit)
        self.assertEqual(self.change(self.payload(can_read_cost=True), self.other_member).status_code, 404)
        self.assertEqual(self.change(self.payload(can_read_cost=True), self.other_member, self.other).status_code, 404)

    def test_removing_debt_visibility_requires_explicit_dependent_permission_confirmation(self):
        response = self.change(self.payload(can_read_debt=False))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["dependent_permissions"], ["can_collect_debt"])
        self.assertFalse(Activity.objects.exists())
        response = self.change(self.payload(can_read_debt=False, confirm_revoke_dependencies=True))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["permissions"]["can_read_debt"])
        self.assertFalse(response.data["permissions"]["can_collect_debt"])

    def test_stale_version_does_not_overwrite_and_invalid_payload_does_not_mutate(self):
        for changes in [{"reason": " "}, {"role": "owner"}, {"user_id": str(self.owner.id)},
                        {"base_version": 0}, {"can_read_cost": "yes"}]:
            self.assertEqual(self.change(self.payload(**changes)).status_code, 400)
        self.assertFalse(Activity.objects.exists())
        self.assertEqual(self.change(self.payload(can_read_cost=True)).status_code, 200)
        self.assertEqual(self.change(self.payload(can_grant_credit=True)).status_code, 409)
        self.member.refresh_from_db()
        self.assertFalse(self.member.can_grant_credit)
        self.assertEqual(Activity.objects.count(), 1)

    def test_membership_removal_blocks_existing_token_in_a_and_keeps_b(self):
        self.assertEqual(self.change(self.payload(is_active=False)).status_code, 200)
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.shop.id}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.other.id}/").status_code, 200)

    def test_retries_recheck_actor_authorization_and_team_read_is_manager_only(self):
        self.authenticate(self.manager)
        data = self.payload(can_grant_credit=True)
        self.assertEqual(self.change(data).status_code, 200)
        self.manager_member.is_active = False
        self.manager_member.save()
        self.assertEqual(self.change(data).status_code, 404)
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.authenticate(self.owner)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["user_id"], str(self.seller.id))

    def test_default_cash_balance_permission_is_hidden_in_verified_lease(self):
        self.authenticate(self.seller)
        response = self.client.post(f"/api/v1/shops/{self.shop.id}/verify-access/", {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.data["permissions"].get("can_read_cash_balance"), False)

    def test_team_list_and_history_are_scoped_and_paginated(self):
        self.assertEqual(self.change(self.payload(can_read_cost=True)).status_code, 200)
        response = self.client.get(f"/api/v1/shops/{self.shop.id}/team/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertNotIn(str(self.other_member.id), {item["id"] for item in response.data["results"]})
        response = self.client.get(self.url() + "history/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        event = response.data["results"][0]
        self.assertEqual(event["actor_id"], str(self.owner.id))
        self.assertTrue(event["after"]["permissions"]["can_read_cost"])
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.url() + "history/").status_code, 403)

    def test_unfold_cannot_bypass_audited_rights_changes_even_for_superuser(self):
        technical = User.objects.create_superuser(username="technical", password="test-only")
        request = RequestFactory().get("/admin/core/membership/")
        request.user = technical
        membership_admin = admin.site._registry[Membership]
        self.assertFalse(membership_admin.has_add_permission(request))
        self.assertFalse(membership_admin.has_change_permission(request, self.member))
        self.assertFalse(membership_admin.has_delete_permission(request, self.member))

    def test_malformed_or_coerced_version_requests_return_400_without_changes(self):
        for data in [[{"role": "manager"}], "role=manager", 3,
                     self.payload(base_version=True, role="manager"),
                     self.payload(base_version=1.0, role="manager"),
                     self.payload(base_version="1", role="manager")]:
            with self.subTest(data=data):
                self.assertEqual(self.change(data).status_code, 400)
        self.assertFalse(Activity.objects.exists())
