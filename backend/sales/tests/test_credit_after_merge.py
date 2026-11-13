"""US-20 / G18: a credit sale recorded offline on a merged customer applies its debt to the merge target."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from catalogue.models import Product
from customers.models import Customer, CustomerDebt
from customers.services.merges import merge_customers
from identity.models import DeviceSession, Installation, Membership, Shop, User


class CreditAfterMergeTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="late-owner")
        self.seller = User.objects.create_user(username="late-seller")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        Membership.objects.create(shop=self.shop, user=self.seller, can_grant_credit=True)
        self.kept = Customer.objects.create(shop=self.shop, name="Awa")
        self.merged = Customer.objects.create(shop=self.shop, name="Awa T.")
        self.product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=10000)
        merge_customers(self.merged, self.kept, actor=self.owner, reason="Même cliente")
        self.client = APIClient()
        device = Installation.objects.create(user=self.seller)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=30))
        access = AccessToken.for_user(self.seller)
        access["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def test_late_credit_sale_on_merged_customer_creates_one_debt_on_target_keeping_origin(self):
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "mode": "credit",
                "customer_id": str(self.merged.id), "payments": [],
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1, "quantity_milli": 1000}]}
        response = self.client.post(f"/api/v1/shops/{self.shop.id}/sales/", data, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["customer_id"], str(self.merged.id))
        debt = CustomerDebt.objects.get()
        self.assertEqual((debt.customer_id, debt.received_for_id), (self.kept.id, self.merged.id))
        self.assertEqual(self.client.post(f"/api/v1/shops/{self.shop.id}/sales/", data, format="json").status_code, 200)
        self.assertEqual(CustomerDebt.objects.count(), 1)
