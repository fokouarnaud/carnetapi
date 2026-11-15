"""US-19/US-21, G6/G7: every change of a debt's remaining amount reaches the
phones allowed to read debts, so a later credit sale can confirm it."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from identity.models import DeviceSession, Installation, Membership, Shop, User


class DebtFeedTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="feed-debt-owner")
        self.seller = User.objects.create_user(username="feed-debt-seller")
        self.shop = Shop.objects.create(name="Boutique", owner=self.owner)
        # A seller with no debt-related permission (collecting is on by
        # default and grants the debt reading it needs).
        self.membership = Membership.objects.create(shop=self.shop, user=self.seller, can_collect_debt=False)
        self.base = f"/api/v1/shops/{self.shop.id}"
        self.client = self.as_user(self.owner)
        self.product_id = str(uuid.uuid4())
        self.assertEqual(self.client.post(f"{self.base}/products/", {
            "id": self.product_id, "operation_id": str(uuid.uuid4()), "name": "Savon", "unit_kind": "piece",
            "unit_label": "pièce", "sale_price_fcfa": 500,
            "opening": {"quantity_milli": 10000, "unit_cost_fcfa": None, "reason": "Stock initial",
                        "occurred_at": (timezone.now() - timedelta(minutes=1)).isoformat()},
        }, format="json").status_code, 201)
        self.customer_id = str(uuid.uuid4())
        self.assertEqual(self.client.post(f"{self.base}/clients/", {
            "id": self.customer_id, "operation_id": str(uuid.uuid4()), "name": "Awa"}, format="json").status_code, 201)
        sale = self.client.post(f"{self.base}/sales/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "mode": "credit",
            "customer_id": self.customer_id, "payments": [],
            "lines": [{"id": str(uuid.uuid4()), "product_id": self.product_id, "product_version": 1,
                       "quantity_milli": 2000}],
        }, format="json")
        self.assertEqual(sale.status_code, 201, sale.data)

    def as_user(self, user):
        client = APIClient()
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(client.post(f"{self.base}/verify-access/", {}, format="json").status_code, 200)
        return client

    def debts(self, client):
        page = client.get(f"{self.base}/changes/?after=0").data["results"]
        return [row for row in page if row["object_type"].startswith("customer_debt_") and row["data"]]

    def test_a_credit_sale_sends_its_debt(self):
        rows = self.debts(self.client)
        self.assertEqual(len(rows), 1)
        data = rows[0]["data"]
        self.assertEqual((data["customer_id"], data["amount_fcfa"], data["remaining_fcfa"]),
                         (self.customer_id, 1000, 1000))

    def test_a_payment_sends_the_reduced_remaining(self):
        debt_id = self.debts(self.client)[0]["data"]["id"]
        paid = self.client.post(f"{self.base}/clients/{self.customer_id}/payments/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "parts": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 400}],
            "allocations": [{"id": str(uuid.uuid4()), "debt_id": debt_id, "amount_fcfa": 400}],
        }, format="json")
        self.assertEqual(paid.status_code, 201, paid.data)
        rows = self.debts(self.client)
        self.assertGreaterEqual(len(rows), 2)
        # Every entry serializes the current debt: 600 still due.
        self.assertEqual({row["data"]["remaining_fcfa"] for row in rows}, {600})

    def test_debts_reach_a_seller_only_with_the_debt_permission(self):
        self.assertEqual(self.debts(self.as_user(self.seller)), [])
        self.membership.can_read_debt = True
        self.membership.save(update_fields=["can_read_debt"])
        self.assertEqual(len(self.debts(self.as_user(self.seller))), 1)
