"""US-10/G6: a pack created or changed on one phone reaches the others."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from identity.models import DeviceSession, Installation, Membership, Shop, User


class PackFeedTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="feed-pack-owner")
        self.seller = User.objects.create_user(username="feed-pack-seller")
        self.shop = Shop.objects.create(name="Boutique", owner=self.owner)
        Membership.objects.create(shop=self.shop, user=self.seller)
        self.base = f"/api/v1/shops/{self.shop.id}"
        self.client = self.as_user(self.owner)
        self.product_id = str(uuid.uuid4())
        created = self.client.post(f"{self.base}/products/", {
            "id": self.product_id, "operation_id": str(uuid.uuid4()), "name": "Riz", "unit_kind": "weight",
            "unit_label": "kg", "sale_price_fcfa": 650, "opening": None,
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.pack_id = str(uuid.uuid4())
        pack = self.client.post(f"{self.base}/products/{self.product_id}/packs/", {
            "id": self.pack_id, "operation_id": str(uuid.uuid4()), "name": "Sac 50 kg",
            "base_quantity_milli": 50000, "sale_price_fcfa": 31000,
        }, format="json")
        self.assertEqual(pack.status_code, 201, pack.data)

    def as_user(self, user):
        client = APIClient()
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(client.post(f"{self.base}/verify-access/", {}, format="json").status_code, 200)
        return client

    def packs(self, client):
        page = client.get(f"{self.base}/changes/?after=0").data["results"]
        return [row for row in page if row["object_type"].startswith("pack_")]

    def test_pack_creation_carries_the_current_pack(self):
        rows = self.packs(self.client)
        self.assertEqual([row["object_type"] for row in rows], ["pack_created"])
        data = rows[0]["data"]
        self.assertEqual((data["id"], data["product_id"], data["name"]), (self.pack_id, self.product_id, "Sac 50 kg"))
        self.assertEqual((data["base_quantity_milli"], data["sale_price_fcfa"], data["is_active"]), (50000, 31000, True))

    def test_a_seller_receives_packs_too(self):
        rows = self.packs(self.as_user(self.seller))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["data"]["id"], self.pack_id)

    def test_a_pack_change_is_sent_with_its_new_values(self):
        changed = self.client.patch(f"{self.base}/products/{self.product_id}/packs/{self.pack_id}/", {
            "operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Prix du sac", "sale_price_fcfa": 30000,
        }, format="json")
        self.assertEqual(changed.status_code, 200, changed.data)
        rows = self.packs(self.client)
        self.assertEqual([row["object_type"] for row in rows], ["pack_created", "pack_updated"])
        # Both entries serialize the current pack, as for products.
        self.assertEqual({row["data"]["sale_price_fcfa"] for row in rows}, {30000})
