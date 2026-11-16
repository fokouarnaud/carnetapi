"""Accountant exports (16 November 2026): sales and purchases journal, in date order."""
from datetime import timedelta
from zoneinfo import ZoneInfo
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from identity.models import DeviceSession, Installation, Membership, Shop, User


class SalesPurchasesJournalTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="sp-owner")
        self.seller = User.objects.create_user(username="sp-seller")
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
        self.customer_id = str(uuid.uuid4())
        self.post("clients/", {"id": self.customer_id, "operation_id": str(uuid.uuid4()), "name": "Awa"})
        self.supplier_id = str(uuid.uuid4())
        self.post("suppliers/", {"id": self.supplier_id, "operation_id": str(uuid.uuid4()), "name": "Grossiste", "phone": None})
        self.today = timezone.now().astimezone(ZoneInfo("Africa/Douala")).date().isoformat()

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

    def sale(self, quantity, **extra):
        return self.post("sales/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "payments": [],
            "lines": [{"id": str(uuid.uuid4()), "product_id": self.product_id, "product_version": 1,
                       "quantity_milli": quantity}], **extra})

    def journal(self, **params):
        return self.client.get(f"{self.base}/accounting/sales-purchases-journal/",
                               {"start": self.today, "end": self.today, **params})

    def test_sales_returns_and_invoices_in_date_order_with_accounts(self):
        self.sale(2000, mode="cash", payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000}])
        credit = self.sale(3000, mode="credit", customer_id=self.customer_id)
        self.post(f"sales/{credit['id']}/returns/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
            "reason": "Article retourné",
            "lines": [{"id": str(uuid.uuid4()), "sale_line_id": credit["lines"][0]["id"], "quantity_milli": 1000}]})
        self.post(f"suppliers/{self.supplier_id}/invoices/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Achat",
            "occurred_at": timezone.now().isoformat(), "external_reference": "F-12",
            "lines": [{"id": str(uuid.uuid4()), "product_id": self.product_id,
                       "quantity_milli": 2000, "unit_price_fcfa": 300}]})
        response = self.journal()
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        rows = [(row["journal"], row["kind"], row["amount_fcfa"], row["party_name"], row["account"], row["party_account"])
                for row in data["results"]]
        self.assertEqual(rows, [
            ("sales", "sale_cash", 1000, "", "701", None),
            ("sales", "sale_credit", 1500, "Awa", "701", "411"),
            ("sales", "sale_return", -500, "Awa", "701", "411"),
            ("purchases", "purchase_invoice", 600, "Grossiste", "601", "401"),
        ])
        self.assertEqual(data["results"][3]["reference"], "F-12")
        self.assertEqual(data["totals"], {"sales": 2500, "sale_returns": 500, "net_sales": 2000,
                                          "purchases": 600, "supplier_credits": 0, "net_purchases": 600})
        self.assertIn("valider", data["accounts_note"])

    def test_outside_the_period_or_for_sellers_nothing(self):
        self.sale(1000, mode="cash", payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 500}])
        self.assertEqual(self.journal(start="2026-01-01", end="2026-01-02").data["count"], 0)
        self.client = self.as_user(self.seller)
        self.assertEqual(self.journal().status_code, 403)
