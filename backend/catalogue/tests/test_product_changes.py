"""US-09: catalogue corrections preserve physical stock and their audit trail."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from identity.models import DeviceSession, Installation, Membership, Shop, User
from stock.models import StockMovement


class ProductChangeTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="changes-owner")
        self.seller = User.objects.create_user(username="changes-seller")
        self.shop = Shop.objects.create(name="Changes", owner=self.owner)
        self.other = Shop.objects.create(name="Other", owner=self.owner)
        self.member = Membership.objects.create(shop=self.shop, user=self.seller)
        self.client = APIClient()
        self.authenticate(self.owner)
        self.creation = {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Bouteille",
            "unit_kind": "piece", "unit_label": "bouteille", "sale_price_fcfa": 250,
            "purchase_price_fcfa": 100,
            "opening": {"quantity_milli": 10000, "unit_cost_fcfa": "100.123456",
                        "occurred_at": "2026-01-01T00:00:00Z", "reason": "Reprise"},
        }
        response = self.client.post(self.base(), self.creation, format="json")
        self.assertEqual(response.status_code, 201)
        self.original_receipt = response.data

    def authenticate(self, user):
        device = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def base(self, shop=None):
        return f"/api/v1/shops/{(shop or self.shop).id}/products/"

    def url(self, shop=None):
        return self.base(shop) + self.creation["id"] + "/"

    def payload(self, **changes):
        return {"operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Correction catalogue", **changes}

    def change(self, data, shop=None):
        return self.client.patch(self.url(shop), data, format="json")

    def test_price_change_preserves_stock_opening_cost_and_original_creation_receipt(self):
        data = self.payload(name="Bouteille grand format", sale_price_fcfa=300, purchase_price_fcfa=None)
        response = self.change(data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["version"], 2)
        self.assertEqual(response.data["sale_price_fcfa"], 300)
        self.assertIsNone(response.data["purchase_price_fcfa"])
        self.assertEqual(response.data["stock_validated_milli"], 10000)
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertEqual(StockMovement.objects.get().unit_cost_micros, 100123456)
        retry = self.change(data)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, response.data)
        original = self.client.post(self.base(), self.creation, format="json")
        self.assertEqual(original.data, self.original_receipt)
        self.assertEqual(original.data["sale_price_fcfa"], 250)
        audit = Activity.objects.get(id=data["operation_id"])
        self.assertEqual(audit.details["before"]["sale_price_fcfa"], 250)
        self.assertEqual(audit.details["after"]["sale_price_fcfa"], 300)
        self.assertEqual(audit.reason, "Correction catalogue")

    def test_archiving_preserves_visibility_history_and_prevents_new_packs(self):
        response = self.change(self.payload(is_active=False))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["is_active"])
        self.assertEqual(StockMovement.objects.count(), 1)
        pack = self.client.post(self.url() + "packs/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Carton",
            "base_quantity_milli": 24000, "sale_price_fcfa": 4800,
        }, format="json")
        self.assertEqual(pack.status_code, 409)
        self.assertEqual(self.client.get(self.url() + "packs/").data["count"], 0)
        self.authenticate(self.seller)
        self.assertFalse(self.client.get(self.url()).data["is_active"])
        self.assertEqual(self.client.get(self.base()).data["count"], 1)
        self.assertNotIn("purchase_price_fcfa", self.client.get(self.url()).data)

    def test_stale_version_and_reused_uuid_do_not_overwrite_confirmed_price(self):
        data = self.payload(sale_price_fcfa=300)
        self.assertEqual(self.change(data).status_code, 200)
        self.assertEqual(self.change(self.payload(sale_price_fcfa=350)).status_code, 409)
        self.assertEqual(self.change({**data, "sale_price_fcfa": 400}).status_code, 409)
        self.assertEqual(self.client.get(self.url()).data["sale_price_fcfa"], 300)
        self.assertEqual(Activity.objects.filter(action="product_updated").count(), 1)

    def test_seller_cannot_change_price_even_with_cost_permission_and_other_shop_is_hidden(self):
        self.member.can_read_cost = True
        self.member.save()
        self.authenticate(self.seller)
        self.assertEqual(self.change(self.payload(sale_price_fcfa=300)).status_code, 403)
        self.assertEqual(self.change(self.payload(sale_price_fcfa=300), self.other).status_code, 404)
        self.authenticate(self.owner)
        self.assertEqual(self.change(self.payload(sale_price_fcfa=300), self.other).status_code, 404)
        self.assertEqual(Activity.objects.count(), 1)

    def test_invalid_changes_cannot_change_unit_stock_identity_or_price_precision(self):
        for changes in [{}, {"reason": " "}, {"name": " "}, {"sale_price_fcfa": True},
                        {"sale_price_fcfa": 249.5}, {"purchase_price_fcfa": -1},
                        {"sale_price_fcfa": 9223372036854775808}, {"is_active": "false"},
                        {"unit_kind": "weight"}, {"stock_validated_milli": 9000},
                        {"id": str(uuid.uuid4())}, {"base_version": 1.0, "name": "Test"}]:
            with self.subTest(changes=changes):
                self.assertEqual(self.change(self.payload(**changes)).status_code, 400)
        self.assertEqual(Activity.objects.count(), 1)
        self.assertEqual(self.client.get(self.url()).data["sale_price_fcfa"], 250)

    def test_product_audit_is_manager_only_and_current_rights_checked_on_retry(self):
        manager = User.objects.create_user(username="changes-manager")
        member = Membership.objects.create(shop=self.shop, user=manager, role="manager")
        self.authenticate(manager)
        data = self.payload(sale_price_fcfa=300)
        self.assertEqual(self.change(data).status_code, 200)
        history = self.client.get(self.url() + "history/")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.data["count"], 2)
        member.is_active = False
        member.save()
        self.assertEqual(self.change(data).status_code, 404)
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.url() + "history/").status_code, 403)
