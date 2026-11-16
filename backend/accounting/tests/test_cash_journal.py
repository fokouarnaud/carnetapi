"""Accountant exports (16 November 2026): the cash journal with suggested SYSCOHADA accounts."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from accounting.models import AccountSuggestion
from identity.models import DeviceSession, Installation, Membership, Shop, User


class CashJournalTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="journal-owner")
        self.seller = User.objects.create_user(username="journal-seller")
        self.shop = Shop.objects.create(owner=self.owner, name="A")
        Membership.objects.create(shop=self.shop, user=self.seller, can_read_cash_balance=True)
        self.client = APIClient()
        self.authenticate(self.owner)

    def authenticate(self, user):
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        access = AccessToken.for_user(user)
        access["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def cash(self, kind, amount, at, method="cash", **extra):
        response = self.client.post(f"/api/v1/shops/{self.shop.id}/cash/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "kind": kind, "method": method,
            "amount_fcfa": amount, "occurred_at": at, "reason": f"{kind} réel",
            "backdate_reason": "Reprise historique", **extra}, format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def journal(self, **params):
        return self.client.get(f"/api/v1/shops/{self.shop.id}/accounting/cash-journal/",
                               {"start": "2026-01-02", "end": "2026-01-03", **params})

    def test_default_accounts_are_suggested_and_marked_to_validate(self):
        self.assertEqual(AccountSuggestion.objects.get(key="method:cash").account, "571")
        self.assertEqual(AccountSuggestion.objects.get(key="expense:transport").account, "618")
        self.assertTrue(all(row.account.isdigit() for row in AccountSuggestion.objects.all()))

    def test_period_in_douala_time_with_opening_balance_totals_and_accounts(self):
        self.cash("opening", 20000, "2026-01-01T10:00:00Z")               # before the period
        self.cash("expense", 1000, "2026-01-01T23:30:00Z", expense_category="transport")  # 2 Jan 00:30 Douala
        self.cash("addition", 5000, "2026-01-03T22:59:00Z")              # 3 Jan 23:59 Douala
        self.cash("withdrawal", 300, "2026-01-03T23:00:00Z")             # 4 Jan, after the period
        self.cash("addition", 700, "2026-01-02T12:00:00Z", method="orange")
        response = self.journal()
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        self.assertEqual((data["period"]["start"], data["period"]["end"], data["period"]["timezone"]),
                         ("2026-01-02", "2026-01-03", "Africa/Douala"))
        self.assertEqual(data["opening_balance_by_method"], {"cash": 20000})
        self.assertEqual(data["totals_by_method"]["cash"], {"in": 5000, "out": 1000})
        self.assertEqual(data["closing_balance_by_method"], {"cash": 24000, "orange": 700})
        self.assertEqual(data["count"], 3)
        first = data["results"][0]
        self.assertEqual((first["local_date"], first["amount_in"], first["amount_out"]), ("2026-01-02", 0, 1000))
        self.assertEqual((first["account"], first["counter_account"]), ("571", "618"))
        self.assertIn("valider", data["accounts_note"])

    def test_counts_and_their_gaps_are_listed_apart(self):
        self.cash("opening", 20000, "2026-01-01T10:00:00Z")
        count = self.client.post(f"/api/v1/shops/{self.shop.id}/cash-counts/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "counted_at": "2026-01-02T18:00:00Z",
            "physical_fcfa": 19850, "method": "cash", "reason": "Comptage"}, format="json")
        self.assertEqual(count.status_code, 201, count.data)
        data = self.journal().data
        self.assertEqual(data["count"], 0)
        gap = data["cash_counts"][0]
        self.assertEqual((gap["expected_fcfa"], gap["physical_fcfa"], gap["difference_fcfa"]), (20000, 19850, -150))
        self.assertEqual(gap["account"], AccountSuggestion.objects.get(key="count_shortage").account)

    def test_reserved_to_managers_and_bounded(self):
        self.authenticate(self.seller)
        self.assertEqual(self.journal().status_code, 403)
        self.authenticate(self.owner)
        for params in [{"start": "2026-01-05", "end": "2026-01-01"}, {"start": "2025-01-01", "end": "2026-12-31"},
                       {"start": "bad"}]:
            with self.subTest(params=params):
                self.assertEqual(self.journal(**params).status_code, 400)
        stranger = User.objects.create_user(username="journal-stranger")
        self.authenticate(stranger)
        self.assertEqual(self.journal().status_code, 404)

    def test_an_edited_account_is_used_at_once(self):
        AccountSuggestion.objects.filter(key="expense:transport").update(account="6181")
        self.cash("expense", 1000, "2026-01-02T08:00:00Z", expense_category="transport")
        self.assertEqual(self.journal().data["results"][0]["counter_account"], "6181")
