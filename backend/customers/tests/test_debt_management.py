"""US-24: openings and deadline changes do not rewrite sales or move money."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from cash.models import CashMovement
from customers.models import Customer, CustomerDebt
from identity.models import DeviceSession, Installation, Membership, Shop, User
from catalogue.models import Product
from sales.models import Sale
from stock.models import StockMovement


class DebtManagementTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="debt-owner")
        self.seller = User.objects.create_user(username="debt-seller")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        self.other = Shop.objects.create(name="B", owner=self.owner)
        self.customer = Customer.objects.create(shop=self.shop, name="Client")
        self.foreign = Customer.objects.create(shop=self.other, name="Client B")
        Membership.objects.create(shop=self.shop, user=self.seller)
        self.client = APIClient()
        self.authenticate(self.owner)

    def authenticate(self, user):
        device = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def base(self, customer=None):
        return f"/api/v1/shops/{self.shop.id}/clients/{(customer or self.customer).id}/debts/"

    def payload(self, **changes):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 10000,
                "occurred_at": "2026-01-01T00:00:00Z", "reason": "Dette antérieure confirmée", **changes}

    def create(self, data=None, customer=None):
        return self.client.post(self.base(customer), data or self.payload(), format="json")

    def test_opening_debt_is_durable_and_idempotent_without_sale_stock_or_cash(self):
        data = self.payload(due_date="2026-10-31")
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data["sale_id"])
        self.assertEqual(response.data["amount_fcfa"], 10000)
        self.assertEqual(response.data["remaining_fcfa"], 10000)
        self.assertEqual(response.data["occurred_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(response.data["due_at"], "2026-10-30T23:00:00Z")
        retry = self.create(data)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, response.data)
        self.assertEqual(CustomerDebt.objects.count(), 1)
        self.assertEqual(Activity.objects.count(), 1)
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(StockMovement.objects.exists())
        self.assertFalse(CashMovement.objects.exists())

    def test_opening_requires_manager_reason_exact_positive_amount_and_past_date(self):
        self.authenticate(self.seller)
        self.assertEqual(self.create().status_code, 403)
        self.authenticate(self.owner)
        for changes in [{"reason": " "}, {"amount_fcfa": True}, {"amount_fcfa": 0}, {"amount_fcfa": 1.5},
                        {"occurred_at": (timezone.now() + timedelta(days=1)).isoformat()}, {"due_date": "bad"}]:
            with self.subTest(changes=changes):
                self.assertEqual(self.create(self.payload(**changes)).status_code, 400)
        self.assertEqual(self.create(customer=self.foreign).status_code, 404)
        self.assertFalse(CustomerDebt.objects.exists())

    def test_change_and_remove_deadline_preserve_principal_and_before_after_journal(self):
        created = self.create()
        self.assertEqual(created.status_code, 201)
        url = self.base() + created.data["id"] + "/due-date/"
        data = {"operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Échéance négociée", "due_date": "2026-10-31"}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["due_version"], 2)
        self.assertEqual(response.data["due_at"], "2026-10-30T23:00:00Z")
        self.assertEqual(self.client.post(url, data, format="json").data, response.data)
        audit = Activity.objects.get(id=data["operation_id"])
        self.assertIsNone(audit.details["before_due_at"])
        self.assertEqual(audit.details["after_due_at"], "2026-10-30T23:00:00Z")
        removed = self.client.post(url, {"operation_id": str(uuid.uuid4()), "base_version": 2,
                                       "reason": "Échéance retirée", "due_date": None}, format="json")
        self.assertEqual(removed.status_code, 200)
        self.assertIsNone(removed.data["due_at"])
        self.assertFalse(removed.data["is_overdue"])
        self.assertEqual(removed.data["remaining_fcfa"], 10000)
        self.assertFalse(CashMovement.objects.exists())

    def test_deadline_changes_require_manager_and_do_not_overwrite_stale_version(self):
        response = self.create()
        self.assertEqual(response.status_code, 201)
        url = self.base() + response.data["id"] + "/due-date/"
        data = {"operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Accord", "due_date": "2026-10-31"}
        self.authenticate(self.seller)
        self.assertEqual(self.client.post(url, data, format="json").status_code, 403)
        self.authenticate(self.owner)
        self.assertEqual(self.client.post(url, data, format="json").status_code, 200)
        self.assertEqual(self.client.post(url, {**data, "operation_id": str(uuid.uuid4()), "due_date": None}, format="json").status_code, 409)
        self.assertEqual(self.client.post(url, {**data, "reason": "Autre"}, format="json").status_code, 409)
        self.assertEqual(CustomerDebt.objects.get().due_version, 2)

    def test_history_is_scoped_paginated_and_manager_only(self):
        response = self.create()
        self.assertEqual(response.status_code, 201)
        url = self.base() + response.data["id"] + "/history/"
        history = self.client.get(url)
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.data["count"], 1)
        self.assertEqual(history.data["results"][0]["reason"], "Dette antérieure confirmée")
        self.assertEqual(self.client.get(self.base(self.foreign) + response.data["id"] + "/history/").status_code, 404)
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_opening_uuid_collision_or_changed_request_does_not_add_debt(self):
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        self.assertEqual(self.create({**data, "amount_fcfa": 20000}).status_code, 409)
        self.assertEqual(self.create({**data, "operation_id": str(uuid.uuid4())}).status_code, 409)
        self.assertEqual(CustomerDebt.objects.get().amount_fcfa, 10000)
        self.assertEqual(Activity.objects.count(), 1)

    def test_sale_deadline_after_collection_preserves_invoice_money_stock_and_original_receipt(self):
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=10000)
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "mode": "credit", "customer_id": str(self.customer.id),
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 2000}]}
        created = self.client.post(f"/api/v1/shops/{self.shop.id}/sales/", data, format="json")
        self.assertEqual(created.status_code, 201)
        debt_id = created.data["debt"]["id"]
        payment = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                   "parts": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000}],
                   "allocations": [{"id": str(uuid.uuid4()), "debt_id": debt_id, "amount_fcfa": 1000}]}
        self.assertEqual(self.client.post(f"/api/v1/shops/{self.shop.id}/clients/{self.customer.id}/payments/", payment, format="json").status_code, 201)
        changed = self.client.post(self.base() + debt_id + "/due-date/", {
            "operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Accord après paiement", "due_date": "2026-10-31",
        }, format="json")
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.data["remaining_fcfa"], 7000)
        self.assertEqual(changed.data["amount_fcfa"], 8000)
        self.assertEqual(Sale.objects.get().net_total_fcfa, 10000)
        self.assertEqual(sorted(CashMovement.objects.values_list("amount_fcfa", flat=True)), [1000, 2000])
        self.assertEqual(StockMovement.objects.get().quantity_milli, -1000)
        self.assertIsNone(Activity.objects.get(id=data["operation_id"]).details["response"]["debt"]["due_at"])
        self.assertEqual(self.client.get(self.base() + debt_id + "/history/").data["count"], 2)
