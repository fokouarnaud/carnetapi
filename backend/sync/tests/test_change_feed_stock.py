"""G6/G7: each stock effect reaches other phones as a current product entry."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from identity.models import DeviceSession, Installation, Shop, User
from sync.models import ChangeFeedEntry


class StockEffectFeedTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="feed-stock-owner")
        self.shop = Shop.objects.create(name="Boutique", owner=self.owner)
        self.client = APIClient()
        installation = Installation.objects.create(user=self.owner)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(self.owner)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        base = f"/api/v1/shops/{self.shop.id}"
        self.assertEqual(self.client.post(f"{base}/verify-access/", {}, format="json").status_code, 200)
        self.product_id = str(uuid.uuid4())
        created = self.client.post(f"{base}/products/", {
            "id": self.product_id, "operation_id": str(uuid.uuid4()), "name": "Savon", "unit_kind": "piece",
            "unit_label": "pièce", "sale_price_fcfa": 500,
            "opening": {"quantity_milli": 10000, "unit_cost_fcfa": None, "reason": "Stock initial",
                        "occurred_at": (timezone.now() - timedelta(minutes=1)).isoformat()},
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.sale = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                     "lines": [{"id": str(uuid.uuid4()), "product_id": self.product_id, "product_version": 1,
                                "quantity_milli": 2000}],
                     "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000}]}
        self.sales_url = f"{base}/sales/"

    def entries(self):
        return list(ChangeFeedEntry.objects.filter(shop=self.shop).order_by("seq")
                    .values_list("object_type", "object_id"))

    def test_opening_stock_adds_no_entry_beyond_the_product_creation(self):
        self.assertEqual(self.entries(), [("product_created", uuid.UUID(self.product_id))])

    def test_a_sale_appends_a_current_stock_entry_after_its_own(self):
        self.assertEqual(self.client.post(self.sales_url, self.sale, format="json").status_code, 201)
        types = [object_type for object_type, _ in self.entries()]
        self.assertEqual(types[-1], "product_stock")
        self.assertLess(types.index("sale_created"), len(types) - 1)
        self.assertEqual(self.entries()[-1][1], uuid.UUID(self.product_id))

        page = self.client.get(f"/api/v1/shops/{self.shop.id}/changes/?after=0").data["results"]
        stock = [row for row in page if row["object_type"] == "product_stock"]
        self.assertEqual(len(stock), 1)
        self.assertEqual(stock[0]["data"]["stock_validated_milli"], 8000)

    def test_a_replayed_sale_appends_nothing(self):
        self.assertEqual(self.client.post(self.sales_url, self.sale, format="json").status_code, 201)
        before = self.entries()
        self.assertEqual(self.client.post(self.sales_url, self.sale, format="json").status_code, 200)
        self.assertEqual(self.entries(), before)
