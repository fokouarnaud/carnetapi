"""US-19/24: credit creates receivables, never fictitious cash."""
from datetime import datetime, timedelta, timezone as utc
import uuid
from unittest.mock import patch

from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from cash.models import CashMovement
from customers.models import Customer
from identity.models import DeviceSession, Installation, Membership, Shop, User
from catalogue.models import Product
from stock.models import StockMovement


class CreditSaleTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="credit-owner")
        self.seller = User.objects.create_user(username="credit-seller")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        self.other = Shop.objects.create(name="B", owner=self.owner)
        self.member = Membership.objects.create(shop=self.shop, user=self.seller, can_grant_credit=True)
        self.customer = Customer.objects.create(shop=self.shop, name="Client sans numéro")
        self.foreign_customer = Customer.objects.create(shop=self.other, name="Client B")
        self.product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=10000)
        self.client = APIClient()
        self.authenticate(self.seller)

    def authenticate(self, user):
        device = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=30))
        access = AccessToken.for_user(user)
        access["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def url(self):
        return f"/api/v1/shops/{self.shop.id}/sales/"

    def debt_url(self, customer=None):
        return f"/api/v1/shops/{self.shop.id}/clients/{(customer or self.customer).id}/debts/"

    def payload(self, **changes):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "mode": "credit",
                "customer_id": str(self.customer.id),
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1, "quantity_milli": 1000}],
                "payments": [], **changes}

    def create(self, data=None):
        return self.client.post(self.url(), data or self.payload(), format="json")

    def test_credit_without_payment_creates_debt_once_and_no_cash_or_customer_account(self):
        data = self.payload()
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["customer_id"], str(self.customer.id))
        self.assertEqual(response.data["debt"]["amount_fcfa"], 10000)
        self.assertEqual(response.data["debt"]["remaining_fcfa"], 10000)
        self.assertIsNone(response.data["debt"]["due_at"])
        self.assertFalse(CashMovement.objects.exists())
        self.assertEqual(User.objects.count(), 2)
        retry = self.create(data)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, response.data)
        debts = self.client.get(self.debt_url())
        self.assertEqual(debts.status_code, 200)
        self.assertEqual(debts.data["count"], 1)
        self.assertFalse(debts.data["results"][0]["is_overdue"])
        self.assertEqual(StockMovement.objects.get(kind="sale").quantity_milli, -1000)

    def test_partial_payment_counts_real_cash_once_and_creates_only_remainder_as_debt(self):
        data = self.payload(payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 3000},
                                     {"id": str(uuid.uuid4()), "method": "mtn", "amount_fcfa": 2000}])
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["net_total_fcfa"], 10000)
        self.assertEqual(response.data["debt"]["amount_fcfa"], 5000)
        self.assertEqual(CashMovement.objects.aggregate(total=Sum("amount_fcfa"))["total"], 5000)
        self.assertEqual(self.create(data).status_code, 200)
        self.assertEqual(CashMovement.objects.count(), 2)

    def test_credit_requires_permission_and_same_shop_customer_without_partial_effect(self):
        self.member.can_grant_credit = False
        self.member.save()
        self.assertEqual(self.create().status_code, 403)
        self.member.can_grant_credit = True
        self.member.save()
        self.assertEqual(self.create(self.payload(customer_id=None)).status_code, 400)
        self.assertEqual(self.create(self.payload(customer_id=str(self.foreign_customer.id))).status_code, 404)
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(StockMovement.objects.exists())
        self.assertFalse(Activity.objects.filter(action="sale_created").exists())

    def test_existing_debt_requires_confirmation_of_current_known_balance(self):
        self.assertEqual(self.create().status_code, 201)
        response = self.create()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["known_debt_fcfa"], 10000)
        self.assertEqual(self.create(self.payload(confirmed_debt_fcfa=9000)).status_code, 409)
        self.assertEqual(self.create(self.payload(confirmed_debt_fcfa=10000)).status_code, 201)
        response = self.create(self.payload(confirmed_debt_fcfa=10000))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["known_debt_fcfa"], 20000)
        self.assertEqual(self.client.get(self.debt_url()).data["count"], 2)

    def test_due_date_is_stored_in_utc_and_overdue_only_next_local_day(self):
        response = self.create(self.payload(due_date="2026-10-31"))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["debt"]["due_at"], "2026-10-30T23:00:00Z")
        with patch("django.utils.timezone.now", return_value=datetime(2026, 10, 31, 22, 59, 59, tzinfo=utc.utc)):
            same_day = self.client.get(self.debt_url())
        self.assertEqual(same_day.status_code, 200)
        self.assertFalse(same_day.data["results"][0]["is_overdue"])
        with patch("django.utils.timezone.now", return_value=datetime(2026, 10, 31, 23, 0, 0, tzinfo=utc.utc)):
            next_day = self.client.get(self.debt_url())
        self.assertEqual(next_day.status_code, 200)
        self.assertTrue(next_day.data["results"][0]["is_overdue"])

    def test_debt_read_requires_effective_permission_and_cannot_download_credit_sales_after_withdrawal(self):
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        self.assertEqual(self.client.get(self.debt_url(self.foreign_customer)).status_code, 404)
        self.member.can_grant_credit = False
        self.member.can_collect_debt = False
        self.member.save()
        self.assertEqual(self.client.get(self.debt_url()).status_code, 403)
        self.assertEqual(self.client.get(self.url() + data["id"] + "/").status_code, 404)
        self.assertEqual(self.client.get(self.url()).data["count"], 0)
        self.member.can_read_debt = True
        self.member.save()
        self.assertEqual(self.client.get(self.debt_url()).status_code, 200)
        self.assertEqual(self.client.get(self.url() + data["id"] + "/").status_code, 200)

    def test_invalid_credit_mode_due_date_and_amount_do_not_create_financial_effects(self):
        for changes in [{"mode": "loan"}, {"due_date": "bad"}, {"confirmed_debt_fcfa": True},
                        {"confirmed_debt_fcfa": 100.5}, {"confirmed_debt_fcfa": -1},
                        {"payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 10001,
                                       "received_fcfa": 10000, "returned_fcfa": 0}]},
                        {"mode": "cash", "due_date": "2026-10-31"}]:
            with self.subTest(changes=changes):
                self.assertEqual(self.create(self.payload(**changes)).status_code, 400)
        self.assertFalse(Activity.objects.filter(action="sale_created").exists())

    def test_due_date_outside_representable_utc_range_is_rejected_without_sale(self):
        self.assertEqual(self.create(self.payload(due_date="0001-01-01")).status_code, 400)
        self.assertFalse(Activity.objects.filter(action="sale_created").exists())
        self.assertFalse(CashMovement.objects.exists())
