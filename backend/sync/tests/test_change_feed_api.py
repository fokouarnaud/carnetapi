"""US-48 / G6: GET /shops/{id}/changes/?after=<seq> is bounded, ordered and shop-scoped."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from identity.models import DeviceSession, Installation, Membership, Shop, User
from sync.services import record_change


class ChangeFeedApiTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="feed-api-owner")
        self.stranger = User.objects.create_user(username="feed-api-stranger")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        self.client = APIClient()

    def login(self, user):
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def url(self, **query):
        suffix = "&".join(f"{key}={value}" for key, value in query.items())
        return f"/api/v1/shops/{self.shop.id}/changes/" + (f"?{suffix}" if suffix else "")

    def test_owner_reads_entries_after_cursor_in_sequence_order(self):
        ids = [uuid.uuid4() for _ in range(3)]
        for object_id in ids:
            record_change(self.shop, "product_created", object_id, "upsert")
        self.login(self.owner)
        response = self.client.get(self.url(after=1))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["seq"] for row in response.data["results"]], [2, 3])
        self.assertEqual(response.data["results"][0]["object_id"], str(ids[1]))
        self.assertEqual(response.data["next_after"], 3)
        self.assertFalse(response.data["has_more"])

    def test_page_is_bounded_and_cursor_resumes_without_gap_or_duplicate(self):
        for _ in range(5):
            record_change(self.shop, "product_created", uuid.uuid4(), "upsert")
        self.login(self.owner)
        first = self.client.get(self.url(after=0, limit=2)).data
        second = self.client.get(self.url(after=first["next_after"], limit=1000)).data
        self.assertEqual([row["seq"] for row in first["results"]], [1, 2])
        self.assertTrue(first["has_more"])
        self.assertEqual([row["seq"] for row in second["results"]], [3, 4, 5])

    def test_non_member_cannot_read_and_invalid_cursor_is_rejected(self):
        record_change(self.shop, "product_created", uuid.uuid4(), "upsert")
        self.login(self.stranger)
        self.assertEqual(self.client.get(self.url(after=0)).status_code, 404)
        Membership.objects.create(shop=self.shop, user=self.stranger)
        self.assertEqual(self.client.get(self.url(after=-1)).status_code, 400)
        self.assertEqual(self.client.get(self.url(after="x")).status_code, 400)

    def test_anonymous_request_is_refused(self):
        self.assertEqual(self.client.get(self.url(after=0)).status_code, 401)


class SellerChangeScopeTests(ChangeFeedApiTests):
    """G7: a seller receives the shared reference data and only their own money operations."""

    def setUp(self):
        super().setUp()
        self.seller = User.objects.create_user(username="feed-seller")
        self.colleague = User.objects.create_user(username="feed-colleague")
        Membership.objects.create(shop=self.shop, user=self.seller)
        Membership.objects.create(shop=self.shop, user=self.colleague)

    def test_seller_sees_catalogue_and_own_sale_but_not_colleague_sale_or_purchases(self):
        product, own_sale, other_sale, purchase = (uuid.uuid4() for _ in range(4))
        record_change(self.shop, "product_created", product, "upsert", actor=self.owner)
        record_change(self.shop, "sale_created", own_sale, "upsert", actor=self.seller)
        record_change(self.shop, "sale_created", other_sale, "upsert", actor=self.colleague)
        record_change(self.shop, "purchase_received", purchase, "upsert", actor=self.owner)
        self.login(self.seller)
        data = self.client.get(self.url(after=0)).data
        self.assertEqual([row["object_id"] for row in data["results"]], [str(product), str(own_sale)])
        self.assertEqual(data["next_after"], 4)

    def test_owner_still_receives_every_entry(self):
        for actor in (self.seller, self.colleague):
            record_change(self.shop, "sale_created", uuid.uuid4(), "upsert", actor=actor)
        self.login(self.owner)
        self.assertEqual(len(self.client.get(self.url(after=0)).data["results"]), 2)
