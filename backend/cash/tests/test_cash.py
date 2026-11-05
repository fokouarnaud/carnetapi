"""US-31: real manual movements and authorized continuous cash ledgers."""
from datetime import timedelta
import uuid
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from cash.models import CashMovement
from identity.models import DeviceSession, Installation, Membership, Shop, User
from catalogue.models import Product
from sales.models import Sale


class CashTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="cash-owner")
        self.seller = User.objects.create_user(username="cash-seller")
        self.shop = Shop.objects.create(owner=self.owner, name="A")
        self.member = Membership.objects.create(shop=self.shop, user=self.seller)
        self.client = APIClient()
        self.authenticate(self.owner)

    def authenticate(self, user):
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        self.device = installation
        self.session = session
        access = AccessToken.for_user(user)
        access["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def url(self, shop=None):
        return f"/api/v1/shops/{(shop or self.shop).id}/cash/"

    def payload(self, **changes):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "kind": "opening", "method": "cash",
                "amount_fcfa": 50000, "occurred_at": "2026-01-01T00:00:00Z", "reason": "Fonds compté", **changes}

    def create(self, data):
        return self.client.post(self.url(), data, format="json")

    def test_opening_addition_withdrawal_expense_keep_continuous_cash_without_sales(self):
        entries = [("opening", 50000, "cash"), ("addition", 10000, "orange"),
                   ("withdrawal", 2000, "cash"), ("expense", 3000, "cash")]
        for kind, amount, method in entries:
            response = self.create(self.payload(kind=kind, amount_fcfa=amount, method=method))
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.data["amount_fcfa"], -amount if kind in ("withdrawal", "expense") else amount)
            self.assertEqual(response.data["actor_id"], str(self.owner.id))
            self.assertEqual(response.data["kind"], kind)
        ledger = self.client.get(self.url(), {"page_size": 1})
        self.assertEqual(ledger.status_code, 200)
        self.assertEqual(ledger.data["count"], 4)
        self.assertEqual(ledger.data["scope"], "global")
        self.assertEqual(ledger.data["totals_by_method_fcfa"], {"cash": 45000, "orange": 10000})
        self.assertTrue(ledger.data["provisional"])
        self.assertEqual(len(ledger.data["results"]), 1)
        self.assertIsNotNone(ledger.data["next"])
        self.assertFalse(Sale.objects.exists())

    def test_retry_preserves_one_movement_and_original_receipt(self):
        data = self.payload()
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        retry = self.create(data)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, response.data)
        self.assertEqual(CashMovement.objects.count(), 1)
        self.assertEqual(Activity.objects.filter(action="cash_manual_created").count(), 1)
        data["amount_fcfa"] = 50001
        self.assertEqual(self.create(data).status_code, 409)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 50000)

    def test_seller_personal_view_does_not_expose_owner_movements_and_global_requires_permission(self):
        self.assertEqual(self.create(self.payload()).status_code, 201)
        self.authenticate(self.seller)
        own = self.client.get(self.url())
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.data["scope"], "personal")
        self.assertEqual(own.data["count"], 0)
        self.assertEqual(own.data["totals_by_method_fcfa"], {})
        self.assertEqual(self.client.get(self.url(), {"scope": "global"}).status_code, 403)
        self.assertEqual(self.create(self.payload()).status_code, 403)
        self.member.can_read_cash_balance = True
        self.member.save()
        global_view = self.client.get(self.url(), {"scope": "global"})
        self.assertEqual(global_view.status_code, 200)
        self.assertEqual(global_view.data["totals_by_method_fcfa"], {"cash": 50000})
        self.assertEqual(self.create(self.payload()).status_code, 403)

    def test_shop_isolation_and_permission_withdrawal_apply_before_totals(self):
        self.assertEqual(self.create(self.payload()).status_code, 201)
        foreign = Shop.objects.create(owner=self.seller, name="B")
        self.assertEqual(self.client.get(self.url(foreign)).status_code, 404)
        self.authenticate(self.seller)
        self.member.is_active = False
        self.member.save()
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_invalid_amounts_kind_future_date_and_missing_reason_leave_no_cash(self):
        for changes in [{"amount_fcfa": True}, {"amount_fcfa": 1.5}, {"amount_fcfa": 0}, {"amount_fcfa": -1},
                        {"kind": "supplier_payment"}, {"reason": " "}, {"occurred_at": "9999-01-01T00:00:00Z"},
                        {"customer_refund": str(uuid.uuid4())}]:
            with self.subTest(changes=changes):
                self.assertEqual(self.create(self.payload(**changes)).status_code, 400)
        self.assertFalse(CashMovement.objects.exists())

    def test_named_other_payment_methods_are_not_combined_into_one_cash_balance(self):
        for name, amount in [("Mobile A", 1000), ("Mobile B", 2000)]:
            self.assertEqual(self.create(self.payload(method="other", other_name=name, amount_fcfa=amount)).status_code, 201)
        ledger = self.client.get(self.url())
        self.assertEqual(ledger.status_code, 200)
        self.assertEqual(ledger.data["totals_by_method_fcfa"], {"other:Mobile A": 1000, "other:Mobile B": 2000})

    def test_snapshot_keeps_totals_and_pages_stable_after_a_late_backdated_movement(self):
        for amount in [1000, 2000]:
            self.assertEqual(self.create(self.payload(amount_fcfa=amount)).status_code, 201)
        first = self.client.get(self.url(), {"page_size": 1})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.create(self.payload(amount_fcfa=4000)).status_code, 201)
        second = self.client.get(first.data["next"])
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["count"], 2)
        self.assertEqual(second.data["as_of"], first.data["as_of"])
        self.assertEqual(second.data["totals_by_method_fcfa"], {"cash": 3000})
        self.assertEqual(self.client.get(self.url()).data["totals_by_method_fcfa"], {"cash": 7000})

    def test_manual_cash_model_rejects_wrong_sign_or_missing_source(self):
        self.assertEqual(self.create(self.payload()).status_code, 201)
        for fields in [{"manual_kind": "expense"}, {"manual_kind": "unknown"}, {"manual_kind": ""},
                       {"amount_fcfa": 0}, {"amount_fcfa": -50000}]:
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), transaction.atomic():
                CashMovement.objects.update(**fields)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 50000)

    def test_seller_personal_ledger_includes_own_sale_payment_without_owner_cash(self):
        self.assertEqual(self.create(self.payload()).status_code, 201)
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1000)
        self.authenticate(self.seller)
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000}]}
        response = self.client.post(f"/api/v1/shops/{self.shop.id}/sales/", data, format="json")
        self.assertEqual(response.status_code, 201)
        own = self.client.get(self.url())
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.data["count"], 1)
        self.assertEqual(own.data["totals_by_method_fcfa"], {"cash": 1000})
        self.assertEqual(own.data["results"][0]["kind"], "sale_payment")
        self.assertEqual(own.data["results"][0]["actor_id"], str(self.seller.id))

    def test_revocation_between_authentication_and_write_blocks_new_cash_and_retries(self):
        from cash.api.cash import create_cash
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        def revoked(*args, **kwargs):
            DeviceSession.objects.filter(id=self.session.id).update(revoked_at=timezone.now())
            return create_cash(*args, **kwargs)
        with patch("cash.api.cash.create_cash", side_effect=revoked):
            response = self.create(data)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(CashMovement.objects.count(), 1)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 50000)

    def test_exact_total_can_exceed_single_sqlite_integer_without_overflow(self):
        for _ in range(2):
            self.assertEqual(self.create(self.payload(amount_fcfa=9223372036854775807)).status_code, 201)
        ledger = self.client.get(self.url(), {"page_size": 1})
        self.assertEqual(ledger.status_code, 200)
        self.assertEqual(ledger.data["totals_by_method_fcfa"], {"cash": 18446744073709551614})
        self.assertEqual(ledger.data["count"], 2)
