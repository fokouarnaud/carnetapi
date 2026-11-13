"""US-48 / E7: a business write and its feed sequence commit or roll back together."""
from datetime import timedelta
import uuid

from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from identity.models import DeviceSession, Installation, Shop, User
from sync.models import ChangeFeedEntry


class ChangeFeedRecordingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="feed-recording-owner")
        self.shop = Shop.objects.create(name="Boutique", owner=self.owner)
        self.client = APIClient()
        installation = Installation.objects.create(user=self.owner)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(self.owner)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def payload(self):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Savon",
                "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 1000}

    def test_product_creation_records_one_entry_and_replay_adds_none(self):
        data = self.payload()
        url = f"/api/v1/shops/{self.shop.id}/products/"
        self.assertEqual(self.client.post(url, data, format="json").status_code, 201)
        self.assertEqual(self.client.post(url, data, format="json").status_code, 200)
        entries = list(ChangeFeedEntry.objects.filter(shop=self.shop).values_list("seq", "object_type", "object_id"))
        self.assertEqual(entries, [(1, "product_created", uuid.UUID(data["id"]))])

    def test_rolled_back_write_leaves_no_entry_and_no_gap(self):
        target = uuid.uuid4()
        try:
            with transaction.atomic():
                Activity.objects.create(shop=self.shop, actor=self.owner, action="product_created", target_id=target)
                raise RuntimeError("panne avant commit")
        except RuntimeError:
            pass
        Activity.objects.create(shop=self.shop, actor=self.owner, action="product_created", target_id=target)
        self.assertEqual(list(ChangeFeedEntry.objects.values_list("seq", flat=True)), [1])
