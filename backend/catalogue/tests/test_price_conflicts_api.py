"""Two current installations retain competing prices and resolve explicitly."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from cash.models import CashMovement
from catalogue.models import Product
from common.models import Activity
from identity.models import DeviceSession, Installation, Membership, Shop, User
from sales.models import Sale, SaleLine
from stock.models import StockMovement


class PriceConflictAPITests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="price-conflict-owner")
        self.seller = User.objects.create_user(username="price-conflict-seller")
        self.shop = Shop.objects.create(name="Prix", owner=self.owner)
        self.other = Shop.objects.create(name="Autre", owner=self.owner)
        Membership.objects.create(shop=self.shop, user=self.seller)
        self.client = APIClient()
        self.authenticate(self.owner)
        self.products = f"/api/v1/shops/{self.shop.id}/products/"
        created = self.client.post(self.products, {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Riz",
            "unit_kind": "piece", "unit_label": "sachet", "sale_price_fcfa": 1000,
            "purchase_price_fcfa": 600,
            "opening": {"quantity_milli": 10000, "unit_cost_fcfa": "600.123456",
                        "occurred_at": "2026-01-01T00:00:00Z", "reason": "Stock physique"},
        }, format="json")
        self.assertEqual(created.status_code, 201)
        self.product_id = created.data["id"]
        self.product_url = f"{self.products}{self.product_id}/"
        self.url = f"{self.product_url}price-conflicts/"
        self.first = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                      "sale_price_fcfa": 1100, "reason": "Premier tarif proposé"}
        changed = self.client.patch(self.product_url, self.first, format="json")
        self.assertEqual(changed.status_code, 200)
        self.authenticate(self.owner)
        self.proposal = {"operation_id": str(uuid.uuid4()),
                         "source_change_id": self.first["operation_id"], "base_version": 1,
                         "proposed_price_fcfa": 1200, "reason": "Autre tarif depuis la même version"}

    def authenticate(self, user):
        self.installation = Installation.objects.create(user=user)
        self.session = DeviceSession.objects.create(
            installation=self.installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(self.session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def post_conflict(self, **changes):
        return self.client.post(self.url, {**self.proposal, **changes}, format="json")

    def sale(self, version, price):
        return self.client.post(f"/api/v1/shops/{self.shop.id}/sales/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "lines": [{"id": str(uuid.uuid4()), "product_id": self.product_id,
                       "product_version": version, "quantity_milli": 1000}],
            "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": price}],
        }, format="json")

    def test_received_conflict_uses_common_price_and_does_not_rewrite_real_sale(self):
        self.assertEqual(self.sale(2, 1100).status_code, 201)
        response = self.post_conflict()
        self.assertEqual(response.status_code, 201)
        conflict = response.json()
        self.assertEqual(conflict["state"], "open")
        self.assertEqual(conflict["common_price_fcfa"], 1000)
        self.assertEqual(sorted(p["proposed_price_fcfa"] for p in conflict["proposals"]), [1100, 1200])
        self.assertEqual(StockMovement.objects.count(), 2)
        self.assertEqual(CashMovement.objects.count(), 1)
        self.assertEqual(self.post_conflict().json(), conflict)
        self.assertEqual(self.post_conflict(proposed_price_fcfa=1250).status_code, 409)
        self.authenticate(self.seller)
        product = self.client.get(self.product_url).json()
        self.assertEqual(product["sale_price_fcfa"], 1000)
        self.assertNotIn("purchase_price_fcfa", product)
        self.assertEqual(product["price_conflict"]["id"], conflict["id"])
        self.assertEqual(product["price_conflict"]["state"], "open")
        self.assertEqual(self.sale(product["version"], 1000).status_code, 201)
        self.assertEqual(sorted(SaleLine.objects.values_list("unit_price_fcfa", flat=True)), [1000, 1100])
        self.assertEqual(sum(CashMovement.objects.values_list("amount_fcfa", flat=True)), 2100)
        self.assertEqual(Sale.objects.count(), 2)

    def test_preview_resolution_requires_current_explicit_choice_and_reason(self):
        response = self.post_conflict()
        self.assertEqual(response.status_code, 201)
        conflict = response.json()
        detail = f"{self.url}{conflict['id']}/"
        preview = self.client.get(detail + "preview/")
        self.assertEqual(preview.status_code, 200)
        proposal = next(p for p in preview.json()["proposals"] if p["proposed_price_fcfa"] == 1200)
        command = {"operation_id": str(uuid.uuid4()), "preview_token": preview.json()["preview_token"],
                   "proposal_id": proposal["id"], "reason": "Tarif retenu après comparaison"}
        self.assertEqual(self.client.post(detail + "resolve/", {**command, "reason": " "}, format="json").status_code, 400)
        resolution = self.client.post(detail + "resolve/", command, format="json")
        self.assertEqual(resolution.status_code, 200)
        self.assertEqual(resolution.json()["state"], "resolved")
        self.assertEqual(resolution.json()["resolution"]["price_fcfa"], 1200)
        self.assertEqual(self.client.post(detail + "resolve/", command, format="json").json(), resolution.json())
        self.assertEqual(self.client.get(self.product_url).json()["sale_price_fcfa"], 1200)
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertEqual(CashMovement.objects.count(), 0)
        self.assertEqual(Activity.objects.filter(action="catalogue_price_resolved").count(), 1)
        history = self.client.get(self.product_url + "history/").json()["results"]
        self.assertIn("catalogue_price_resolved", [event["action"] for event in history])

    def test_new_proposal_invalidates_preview_and_native_price_cannot_bypass_conflict(self):
        response = self.post_conflict()
        self.assertEqual(response.status_code, 201)
        conflict = response.json()
        detail = f"{self.url}{conflict['id']}/"
        preview = self.client.get(detail + "preview/").json()
        product = self.client.get(self.product_url).json()
        bypass = self.client.patch(self.product_url, {"operation_id": str(uuid.uuid4()),
            "base_version": product["version"], "sale_price_fcfa": 5000, "reason": "Contournement"}, format="json")
        self.assertEqual(bypass.status_code, 409)
        third = self.post_conflict(operation_id=str(uuid.uuid4()), proposed_price_fcfa=1300)
        self.assertEqual(third.status_code, 201)
        self.assertEqual(len(third.json()["proposals"]), 3)
        stale = self.client.post(detail + "resolve/", {"operation_id": str(uuid.uuid4()),
            "preview_token": preview["preview_token"], "proposal_id": None, "reason": "Ancien aperçu"}, format="json")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.client.get(self.product_url).json()["sale_price_fcfa"], 1000)
        self.assertEqual(self.client.get(self.url).json()["count"], 1)

    def test_current_rights_isolation_and_strict_validation(self):
        for values in [{"proposed_price_fcfa": True}, {"proposed_price_fcfa": 1.5},
                       {"proposed_price_fcfa": -1}, {"reason": " "}, {"base_version": 1.0},
                       {"proposed_price_fcfa": 9223372036854775808}, {"unknown": 1}]:
            with self.subTest(values=values):
                self.assertEqual(self.post_conflict(**values).status_code, 400)
        foreign = self.url.replace(str(self.shop.id), str(self.other.id))
        self.assertEqual(self.client.post(foreign, self.proposal, format="json").status_code, 404)
        self.authenticate(self.seller)
        self.assertEqual(self.post_conflict().status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.authenticate(self.owner)
        self.session.revoked_at = timezone.now()
        self.session.save(update_fields=["revoked_at"])
        self.assertEqual(self.post_conflict().status_code, 401)
        self.assertEqual(Product.objects.get(id=self.product_id).sale_price_fcfa, 1100)
