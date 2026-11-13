"""US-48 / G6-G7: feed entries carry the object's current representation, filtered by the reader's rights."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from identity.models import DeviceSession, Installation, Membership, Shop, User


class ChangeFeedPayloadTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="payload-owner")
        self.seller = User.objects.create_user(username="payload-seller")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        Membership.objects.create(shop=self.shop, user=self.seller)
        self.client = APIClient()
        self.login(self.owner)
        self.product_id = str(uuid.uuid4())
        created = self.client.post(f"/api/v1/shops/{self.shop.id}/products/", {
            "id": self.product_id, "operation_id": str(uuid.uuid4()), "name": "Savon", "unit_kind": "piece",
            "unit_label": "pièce", "sale_price_fcfa": 1000, "purchase_price_fcfa": 600}, format="json")
        self.assertEqual(created.status_code, 201, created.content)

    def login(self, user):
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def feed(self):
        response = self.client.get(f"/api/v1/shops/{self.shop.id}/changes/?after=0")
        self.assertEqual(response.status_code, 200)
        return response.data["results"]

    def test_owner_receives_current_product_with_cost(self):
        entry = self.feed()[0]
        self.assertEqual((entry["data"]["id"], entry["data"]["name"]), (self.product_id, "Savon"))
        self.assertEqual(entry["data"]["purchase_price_fcfa"], 600)

    def test_seller_receives_product_without_cost(self):
        self.login(self.seller)
        entry = self.feed()[0]
        self.assertEqual(entry["data"]["sale_price_fcfa"], 1000)
        self.assertNotIn("purchase_price_fcfa", entry["data"])


class CustomerFeedPayloadTests(ChangeFeedPayloadTests):
    def test_customer_entry_carries_the_current_customer(self):
        customer_id = str(uuid.uuid4())
        created = self.client.post(f"/api/v1/shops/{self.shop.id}/clients/", {
            "id": customer_id, "operation_id": str(uuid.uuid4()), "name": "Awa"}, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        self.login(self.seller)
        entry = next(row for row in self.feed() if row["object_type"] == "client_created")
        self.assertEqual((entry["data"]["id"], entry["data"]["name"]), (customer_id, "Awa"))


class SaleFeedPayloadTests(ChangeFeedPayloadTests):
    def test_seller_receives_the_content_of_their_own_cash_sale(self):
        self.login(self.seller)
        sale_id = str(uuid.uuid4())
        sold = self.client.post(f"/api/v1/shops/{self.shop.id}/sales/", {
            "id": sale_id, "operation_id": str(uuid.uuid4()), "payments": [
                {"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000}],
            "lines": [{"id": str(uuid.uuid4()), "product_id": self.product_id, "product_version": 1, "quantity_milli": 1000}]},
            format="json")
        self.assertEqual(sold.status_code, 201, sold.content)
        entry = next(row for row in self.feed() if row["object_type"] == "sale_created")
        self.assertEqual((entry["data"]["id"], entry["data"]["net_total_fcfa"]), (sale_id, 1000))


class DebtFeedPayloadTests(ChangeFeedPayloadTests):
    def open_debt(self):
        from customers.models import Customer
        customer = Customer.objects.create(shop=self.shop, name="Awa")
        debt_id = str(uuid.uuid4())
        opened = self.client.post(f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/debts/", {
            "id": debt_id, "operation_id": str(uuid.uuid4()), "amount_fcfa": 5000,
            "occurred_at": timezone.now().isoformat(), "reason": "Solde antérieur"}, format="json")
        self.assertEqual(opened.status_code, 201, opened.content)
        return debt_id

    def debt_entries(self):
        return [row for row in self.feed() if row["object_type"] == "customer_debt_opened"]

    def test_seller_with_debt_permission_receives_the_debt(self):
        debt_id = self.open_debt()
        Membership.objects.filter(shop=self.shop, user=self.seller).update(can_read_debt=True)
        self.login(self.seller)
        entry = self.debt_entries()[0]
        self.assertEqual((entry["data"]["id"], entry["data"]["remaining_fcfa"]), (debt_id, 5000))

    def test_seller_without_debt_permission_receives_no_debt(self):
        self.open_debt()
        Membership.objects.filter(shop=self.shop, user=self.seller).update(can_read_debt=False, can_collect_debt=False,
                                                                        can_grant_credit=False, can_prepare_reminder=False)
        self.login(self.seller)
        self.assertEqual(self.debt_entries(), [])
