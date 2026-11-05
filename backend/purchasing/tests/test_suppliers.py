"""Supplier identities are per shop and do not require an application account."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from cash.models import CashMovement
from identity.models import DeviceSession, Installation, Membership, Shop, User
from stock.models import StockMovement


class SupplierTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="supplier-owner")
        self.seller = User.objects.create_user(username="supplier-seller")
        self.shop = Shop.objects.create(owner=self.owner, name="A")
        Membership.objects.create(shop=self.shop, user=self.seller)
        self.client = APIClient()
        self.authenticate(self.owner)

    def authenticate(self, user):
        device = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def url(self, shop=None):
        return f"/api/v1/shops/{(shop or self.shop).id}/suppliers/"

    def payload(self, **changes):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Fournisseur", "phone": None, **changes}

    def test_creation_and_retry_keep_one_supplier_without_financial_or_account_effect(self):
        data = self.payload()
        response = self.client.post(self.url(), data, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["id"], data["id"])
        self.assertEqual(response.data["name"], "Fournisseur")
        retry = self.client.post(self.url(), data, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, response.data)
        self.assertEqual(self.client.get(self.url()).data["count"], 1)
        self.assertEqual(Activity.objects.filter(action="supplier_created").count(), 1)
        self.assertEqual(User.objects.count(), 2)
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

    def test_seller_cannot_create_or_download_supplier_details(self):
        self.authenticate(self.seller)
        self.assertEqual(self.client.post(self.url(), self.payload(), format="json").status_code, 403)
        self.assertEqual(self.client.get(self.url()).status_code, 403)

    def test_supplier_identity_is_isolated_per_shop_without_automatic_name_merging(self):
        other = Shop.objects.create(owner=self.owner, name="B")
        first = self.client.post(self.url(), self.payload(), format="json")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(self.client.get(self.url(other)).data["count"], 0)
        second = self.client.post(self.url(), self.payload(), format="json")
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.data["id"], second.data["id"])
        self.assertEqual(self.client.get(self.url()).data["count"], 2)
