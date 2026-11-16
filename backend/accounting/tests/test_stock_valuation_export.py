"""Accountant exports (16 November 2026): stock valued at moving average cost at a closing date,
unknown costs flagged, losses of the period apart."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from identity.models import DeviceSession, Installation, Membership, Shop, User


class StockValuationExportTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="sv-owner")
        self.seller = User.objects.create_user(username="sv-seller")
        self.shop = Shop.objects.create(name="Boutique", owner=self.owner)
        Membership.objects.create(shop=self.shop, user=self.seller)
        self.base = f"/api/v1/shops/{self.shop.id}"
        self.client = self.as_user(self.owner)

    def as_user(self, user):
        client = APIClient()
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(client.post(f"{self.base}/verify-access/", {}, format="json").status_code, 200)
        return client

    def product(self, name, quantity, cost):
        product_id = str(uuid.uuid4())
        response = self.client.post(f"{self.base}/products/", {
            "id": product_id, "operation_id": str(uuid.uuid4()), "name": name, "unit_kind": "piece",
            "unit_label": "pièce", "sale_price_fcfa": 1000,
            "opening": {"quantity_milli": quantity, "unit_cost_fcfa": cost, "reason": "Stock initial",
                        "occurred_at": "2026-01-01T08:00:00Z"},
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return product_id

    def export(self, **params):
        return self.client.get(f"{self.base}/accounting/stock/", {"start": "2026-01-01", "end": "2026-01-31", **params})

    def test_values_at_the_date_flag_unknown_costs_and_list_losses_apart(self):
        rice = self.product("Riz", 10000, 600)
        self.product("Sel", 4000, None)
        loss = self.client.post(f"{self.base}/products/{rice}/losses/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "quantity_milli": 2000,
            "occurred_at": "2026-01-10T10:00:00Z", "backdate_reason": "Reprise historique", "reason": "Sacs mouillés"},
            format="json")
        self.assertEqual(loss.status_code, 201, loss.data)
        response = self.export()
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        rows = {row["name"]: row for row in data["results"]}
        self.assertEqual((rows["Riz"]["quantity_milli"], rows["Riz"]["value_fcfa"], rows["Riz"]["value_state"]),
                         ("8000", 4800, "known"))
        self.assertEqual(rows["Riz"]["account"], "311")
        self.assertEqual((rows["Sel"]["value_fcfa"], rows["Sel"]["value_state"]), (None, "unknown"))
        self.assertEqual((data["totals"]["known_value_fcfa"], data["totals"]["unknown_count"]), (4800, 1))
        self.assertEqual([(row["name"], row["quantity_milli"], row["value_fcfa"], row["account"]) for row in data["losses"]],
                         [("Riz", "2000", 1200, "6031")])
        before = self.export(start="2025-12-01", end="2025-12-31").data
        self.assertEqual((before["count"], before["losses"]), (0, []))

    def test_reserved_to_managers(self):
        self.client = self.as_user(self.seller)
        self.assertEqual(self.export().status_code, 403)
