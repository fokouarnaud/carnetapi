from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from identity.models import DeviceSession, Installation, Membership, Shop, User


class ShopAccessTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner")
        self.seller = User.objects.create_user(username="seller")
        self.other = User.objects.create_user(username="other")
        self.a = Shop.objects.create(name="A", owner=self.owner)
        self.b = Shop.objects.create(name="B", owner=self.other)
        self.membership = Membership.objects.create(shop=self.a, user=self.seller)
        self.client = APIClient()

    def authenticate(self, user):
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_anonymous_cannot_list_shops(self):
        self.assertEqual(self.client.get("/api/v1/shops/").status_code, 401)

    def test_list_filters_before_paginating_and_counting(self):
        self.authenticate(self.seller)
        response = self.client.get("/api/v1/shops/?page_size=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual([r["id"] for r in response.data["results"]], [str(self.a.id)])
        self.assertIsNone(response.data["next"])

    def test_known_uuid_does_not_grant_cross_shop_access(self):
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.a.id}/").status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.b.id}/").status_code, 404)

    def test_owner_has_access_without_an_explicit_membership(self):
        self.authenticate(self.owner)
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.a.id}/").status_code, 200)

    def test_revoked_membership_blocks_an_already_issued_access_token(self):
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.a.id}/").status_code, 200)
        self.membership.is_active = False
        self.membership.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.a.id}/").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/shops/").data["count"], 0)

    def test_deactivated_account_cannot_use_existing_token(self):
        self.authenticate(self.seller)
        self.seller.is_active = False
        self.seller.save(update_fields=["is_active"])
        self.assertEqual(self.client.get("/api/v1/shops/").status_code, 401)

    def test_business_owner_cannot_read_schema(self):
        self.authenticate(self.owner)
        self.assertEqual(self.client.get("/api/v1/schema/").status_code, 403)

    def test_staff_alone_cannot_read_schema(self):
        self.seller.is_staff = True
        self.seller.save(update_fields=["is_staff"])
        self.authenticate(self.seller)
        self.assertEqual(self.client.get("/api/v1/schema/").status_code, 403)

    def test_documentation_interfaces_have_the_same_technical_gate(self):
        self.authenticate(self.owner)
        for path in ["/api/v1/docs/", "/api/v1/redoc/"]:
            self.assertEqual(self.client.get(path).status_code, 403)
        self.owner.is_superuser = True
        self.owner.save(update_fields=["is_superuser"])
        self.authenticate(self.owner)
        for path in ["/api/v1/docs/", "/api/v1/redoc/"]:
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_archived_shop_is_not_readable_even_by_owner(self):
        self.a.is_active = False
        self.a.save(update_fields=["is_active"])
        self.authenticate(self.owner)
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.a.id}/").status_code, 404)

    def test_superuser_can_read_schema(self):
        self.owner.is_superuser = True
        self.owner.save(update_fields=["is_superuser"])
        self.authenticate(self.owner)
        self.assertEqual(self.client.get("/api/v1/schema/").status_code, 200)

    def test_page_size_is_bounded_without_dropping_records(self):
        Shop.objects.bulk_create([Shop(name=f"Shop {n}", owner=self.owner) for n in range(105)])
        self.authenticate(self.owner)
        response = self.client.get("/api/v1/shops/?page_size=999999")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 106)
        self.assertEqual(len(response.data["results"]), 100)
        second = self.client.get(response.data["next"])
        self.assertEqual(len(second.data["results"]), 6)
        first_ids = {r["id"] for r in response.data["results"]}
        self.assertFalse(first_ids & {r["id"] for r in second.data["results"]})
