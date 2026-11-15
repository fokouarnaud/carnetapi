"""US-27/US-29, G6/G7: suppliers and what they are owed reach managers'
phones; sellers never receive purchase data."""
from datetime import timedelta
import importlib
import uuid

from django.apps import apps
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from identity.models import DeviceSession, Installation, Membership, Shop, User
from sync.models import ChangeFeedEntry, ShopChangeSequence


class SupplierFeedTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="feed-supplier-owner")
        self.seller = User.objects.create_user(username="feed-supplier-seller")
        self.shop = Shop.objects.create(name="Boutique", owner=self.owner)
        Membership.objects.create(shop=self.shop, user=self.seller)
        self.base = f"/api/v1/shops/{self.shop.id}"
        self.client = self.as_user(self.owner)
        self.product_id = str(uuid.uuid4())
        self.assertEqual(self.client.post(f"{self.base}/products/", {
            "id": self.product_id, "operation_id": str(uuid.uuid4()), "name": "Riz", "unit_kind": "piece",
            "unit_label": "sac", "sale_price_fcfa": 30000,
            "opening": {"quantity_milli": 0, "unit_cost_fcfa": None, "reason": "Stock initial",
                        "occurred_at": (timezone.now() - timedelta(minutes=2)).isoformat()},
        }, format="json").status_code, 201)
        self.supplier_id = str(uuid.uuid4())
        created = self.client.post(f"{self.base}/suppliers/", {
            "id": self.supplier_id, "operation_id": str(uuid.uuid4()), "name": "Grossiste", "phone": None,
        }, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.invoice_id = str(uuid.uuid4())
        invoice = self.client.post(f"{self.base}/suppliers/{self.supplier_id}/invoices/", {
            "id": self.invoice_id, "operation_id": str(uuid.uuid4()), "reason": "Achat",
            "occurred_at": (timezone.now() - timedelta(minutes=1)).isoformat(),
            "lines": [{"id": str(uuid.uuid4()), "product_id": self.product_id,
                       "quantity_milli": 2000, "unit_price_fcfa": 25000}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)

    def as_user(self, user):
        client = APIClient()
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(client.post(f"{self.base}/verify-access/", {}, format="json").status_code, 200)
        return client

    def rows(self, client, prefix):
        page = client.get(f"{self.base}/changes/?after=0").data["results"]
        return [row for row in page if row["object_type"].startswith(prefix) and row["data"]]

    def test_a_manager_receives_the_supplier_and_its_debt(self):
        suppliers = [row for row in self.rows(self.client, "supplier_") if row["object_type"] == "supplier_created"]
        self.assertEqual([row["data"]["name"] for row in suppliers], ["Grossiste"])
        debts = self.rows(self.client, "supplier_debt_")
        self.assertEqual(len(debts), 1)
        data = debts[0]["data"]
        self.assertEqual((data["supplier_id"], data["document_id"], data["amount_fcfa"], data["remaining_fcfa"]),
                         (self.supplier_id, self.invoice_id, 50000, 50000))

    def test_a_seller_receives_no_supplier_data(self):
        seller = self.as_user(self.seller)
        page = seller.get(f"{self.base}/changes/?after=0").data["results"]
        self.assertFalse([row for row in page if row["object_type"].startswith(("supplier_", "purchase_"))])

    def test_older_supplier_debts_are_backfilled_once(self):
        backfill = importlib.import_module("sync.migrations.0003_backfill_supplier_debt_states").backfill
        ChangeFeedEntry.objects.filter(object_type="supplier_debt_state").delete()
        last = ShopChangeSequence.objects.get(shop=self.shop).last_seq
        backfill(apps, None)
        debts = self.rows(self.client, "supplier_debt_")
        self.assertEqual([row["seq"] for row in debts], [last + 1])
        backfill(apps, None)
        self.assertEqual(ChangeFeedEntry.objects.filter(object_type="supplier_debt_state").count(), 1)

    def test_a_phone_clock_slightly_ahead_is_the_present(self):
        def invoice(ahead):
            return self.client.post(f"{self.base}/suppliers/{self.supplier_id}/invoices/", {
                "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Achat",
                "occurred_at": (timezone.now() + ahead).isoformat(),
                "lines": [{"id": str(uuid.uuid4()), "product_id": self.product_id,
                           "quantity_milli": 1000, "unit_price_fcfa": 25000}],
            }, format="json")
        accepted = invoice(timedelta(seconds=20))
        self.assertEqual(accepted.status_code, 201, accepted.data)
        from datetime import datetime
        self.assertLessEqual(datetime.fromisoformat(accepted.data["occurred_at"].replace("Z", "+00:00")),
                             timezone.now())
        self.assertEqual(invoice(timedelta(minutes=10)).status_code, 400)

    def test_an_invoice_debt_is_named_after_the_invoice(self):
        debt = self.rows(self.client, "supplier_debt_")[0]["data"]
        self.assertEqual(debt["id"], str(uuid.uuid5(uuid.UUID(self.invoice_id), "supplier-debt")))
