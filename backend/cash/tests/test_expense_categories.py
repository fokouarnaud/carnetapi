"""US-31 / G23: every expense carries one fixed category; "other" needs a label."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from cash.models import CashMovement
from identity.models import DeviceSession, Installation, Shop, User


class ExpenseCategoryTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="expense-owner")
        self.shop = Shop.objects.create(owner=self.owner, name="A")
        self.client = APIClient()
        installation = Installation.objects.create(user=self.owner)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        access = AccessToken.for_user(self.owner)
        access["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def create(self, **changes):
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "kind": "expense", "method": "cash",
                "amount_fcfa": 2000, "occurred_at": timezone.now().isoformat(), "reason": "Taxi livraison", **changes}
        return self.client.post(f"/api/v1/shops/{self.shop.id}/cash/", data, format="json")

    def test_expense_with_fixed_category_is_recorded_and_returned(self):
        response = self.create(expense_category="transport")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["expense_category"], "transport")
        self.assertEqual(CashMovement.objects.get().expense_category, "transport")

    def test_expense_without_or_with_unknown_category_is_refused_without_cash(self):
        for changes in ({}, {"expense_category": "fete"}, {"expense_category": "other"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.create(**changes).status_code, 400)
        self.assertFalse(CashMovement.objects.exists())

    def test_other_category_requires_its_label_and_non_expense_refuses_a_category(self):
        response = self.create(expense_category="other", expense_label="Réparation porte")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["expense_label"], "Réparation porte")
        self.assertEqual(self.create(kind="addition", expense_category="transport").status_code, 400)
