"""Accountant exports (16 November 2026): customer and supplier balances at a closing date."""
from datetime import timedelta
from zoneinfo import ZoneInfo
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from identity.models import DeviceSession, Installation, Membership, Shop, User


class PartyBalancesTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="bal-owner")
        self.seller = User.objects.create_user(username="bal-seller")
        self.shop = Shop.objects.create(name="Boutique", owner=self.owner)
        Membership.objects.create(shop=self.shop, user=self.seller)
        self.base = f"/api/v1/shops/{self.shop.id}"
        self.client = self.as_user(self.owner)
        self.product_id = str(uuid.uuid4())
        self.post("products/", {
            "id": self.product_id, "operation_id": str(uuid.uuid4()), "name": "Savon", "unit_kind": "piece",
            "unit_label": "pièce", "sale_price_fcfa": 500,
            "opening": {"quantity_milli": 10000, "unit_cost_fcfa": None, "reason": "Stock initial",
                        "occurred_at": (timezone.now() - timedelta(minutes=5)).isoformat()}})
        self.today = timezone.now().astimezone(ZoneInfo("Africa/Douala")).date()

    def as_user(self, user):
        client = APIClient()
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(client.post(f"{self.base}/verify-access/", {}, format="json").status_code, 200)
        return client

    def post(self, path, body):
        response = self.client.post(f"{self.base}/{path}", body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def customer(self, name):
        customer_id = str(uuid.uuid4())
        self.post("clients/", {"id": customer_id, "operation_id": str(uuid.uuid4()), "name": name})
        return customer_id

    def credit_sale(self, customer_id, quantity):
        self.post("sales/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "payments": [], "mode": "credit",
            "customer_id": customer_id,
            "lines": [{"id": str(uuid.uuid4()), "product_id": self.product_id, "product_version": 1,
                       "quantity_milli": quantity}]})

    def balances(self, party, day=None):
        return self.client.get(f"{self.base}/accounting/balances/",
                               {"party": party, "date": (day or self.today).isoformat()})

    def test_customers_owing_at_the_date_with_accounts_and_totals(self):
        awa, ben, _nobody = self.customer("Awa"), self.customer("Ben"), self.customer("Sans dette")
        self.credit_sale(awa, 2000)   # 1000
        self.credit_sale(ben, 1000)   # 500
        from customers.models import CustomerDebt
        debt_id = str(CustomerDebt.objects.get(customer_id=awa).id)
        self.post(f"clients/{awa}/payments/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "parts": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 400}],
            "allocations": [{"id": str(uuid.uuid4()), "debt_id": debt_id, "amount_fcfa": 400}]})
        response = self.balances("customers")
        self.assertEqual(response.status_code, 200, response.data)
        rows = [(row["name"], row["debt_fcfa"], row["account"]) for row in response.data["results"]]
        self.assertEqual(rows, [("Awa", 600, "411"), ("Ben", 500, "411")])
        self.assertEqual(response.data["totals"]["debt_fcfa"], 1100)
        self.assertEqual(response.data["date"], self.today.isoformat())
        self.assertIn("valider", response.data["accounts_note"])
        yesterday = self.balances("customers", self.today - timedelta(days=1)).data
        self.assertEqual((yesterday["count"], yesterday["totals"]["debt_fcfa"]), (0, 0))

    def test_suppliers_owed_and_reserved_to_managers(self):
        supplier_id = str(uuid.uuid4())
        self.post("suppliers/", {"id": supplier_id, "operation_id": str(uuid.uuid4()), "name": "Grossiste", "phone": None})
        self.post(f"suppliers/{supplier_id}/invoices/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Achat",
            "occurred_at": timezone.now().isoformat(),
            "lines": [{"id": str(uuid.uuid4()), "product_id": self.product_id, "quantity_milli": 2000,
                       "unit_price_fcfa": 300}]})
        data = self.balances("suppliers").data
        self.assertEqual([(row["name"], row["debt_fcfa"], row["account"]) for row in data["results"]],
                         [("Grossiste", 600, "401")])
        self.assertEqual(self.balances("other").status_code, 400)
        self.client = self.as_user(self.seller)
        self.assertEqual(self.balances("customers").status_code, 403)
