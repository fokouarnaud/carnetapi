"""US-32: physical counts survive late cash events without an invented correction."""
from datetime import timedelta
import uuid
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from cash.models import CashCount, CashMovement
from identity.models import DeviceSession, Installation, Membership, Shop, User


class CashCountTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="count-owner")
        self.seller = User.objects.create_user(username="count-seller")
        self.shop = Shop.objects.create(owner=self.owner, name="A")
        Membership.objects.create(shop=self.shop, user=self.seller, can_read_cash_balance=True)
        self.client = APIClient()
        self.authenticate(self.owner)

    def authenticate(self, user):
        device = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=30))
        self.session = session
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def url(self):
        return f"/api/v1/shops/{self.shop.id}/cash-counts/"

    def cash(self, amount, at="2026-01-01T00:00:00Z", kind="opening", revises=None):
        response = self.client.post(f"/api/v1/shops/{self.shop.id}/cash/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "kind": kind, "method": "cash",
            "amount_fcfa": amount, "occurred_at": at, "reason": "Mouvement réel", "backdate_reason": "Reprise historique",  # G15
            # G23: an expense always carries a fixed category.
            **({"expense_category": "supplies"} if kind == "expense" else {}),
            # G15: a movement dated before a validated count names the count it revises.
            **({"revises_count_id": revises} if revises else {})}, format="json")
        self.assertEqual(response.status_code, 201)

    def payload(self, **changes):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "counted_at": "2026-01-02T00:00:00Z",
                "physical_fcfa": 48000, "method": "cash", "reason": "Comptage physique", **changes}

    def test_count_preserves_expected_physical_and_minus_2000_without_cash_adjustment(self):
        self.cash(50000)
        data = self.payload()
        response = self.client.post(self.url(), data, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["expected_fcfa"], 50000)
        self.assertEqual(response.data["physical_fcfa"], 48000)
        self.assertEqual(response.data["difference_fcfa"], -2000)
        self.assertEqual(response.data["expected_at_creation_fcfa"], 50000)
        self.assertIn("recorded_at", response.data)
        self.assertGreater(parse_datetime(response.data["recorded_at"]), parse_datetime(response.data["counted_at"]))
        self.assertTrue(response.data["provisional"])
        self.assertTrue(response.data["requires_regularization"])
        retry = self.client.post(self.url(), data, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, response.data)
        self.assertEqual(CashMovement.objects.count(), 1)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 50000)

    def test_late_prior_movement_revises_expected_but_never_physical_or_original_receipt(self):
        self.cash(50000)
        data = self.payload()
        initial = self.client.post(self.url(), data, format="json")
        self.assertEqual(initial.status_code, 201)
        self.cash(7000, at="2026-01-03T00:00:00Z", kind="addition")
        self.cash(3000, kind="expense", revises=data["id"])
        count = self.client.get(self.url() + data["id"] + "/")
        self.assertEqual(count.status_code, 200)
        self.assertEqual(count.data["expected_fcfa"], 47000)
        self.assertEqual(count.data["physical_fcfa"], 48000)
        self.assertEqual(count.data["difference_fcfa"], 1000)
        self.assertEqual(count.data["expected_at_creation_fcfa"], 50000)
        self.assertTrue(count.data["expected_revised"])
        self.assertEqual(self.client.post(self.url(), data, format="json").data, initial.data)
        self.assertEqual(CashMovement.objects.count(), 3)

    def test_seller_cash_permission_does_not_grant_physical_count_management(self):
        self.authenticate(self.seller)
        self.assertEqual(self.client.post(self.url(), self.payload(), format="json").status_code, 403)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.assertFalse(CashMovement.objects.exists())

    def test_database_preserves_integer_expected_balance_even_beyond_int64(self):
        for _ in range(2):
            self.cash(9223372036854775807)
        response = self.client.post(self.url(), self.payload(), format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["expected_at_creation_fcfa"], 18446744073709551614)
        self.assertEqual(CashCount.objects.get().expected_at_creation_fcfa, 18446744073709551614)
        for value in [True, 1.5, "50000", {}, []]:
            with self.subTest(value=value), self.assertRaises(IntegrityError), transaction.atomic():
                CashCount.objects.update(expected_at_creation_fcfa=value)
        self.assertEqual(CashCount.objects.get().expected_at_creation_fcfa, 18446744073709551614)

    def test_session_withdrawn_after_authentication_cannot_read_physical_counts(self):
        response = self.client.post(self.url(), self.payload(), format="json")
        self.assertEqual(response.status_code, 201)
        from cash.api.cash_counts import CashCountViewSet
        original = CashCountViewSet.get_queryset
        def revoked(view):
            DeviceSession.objects.filter(id=self.session.id).update(revoked_at=timezone.now())
            return original(view)
        with patch.object(CashCountViewSet, "get_queryset", revoked):
            response = self.client.get(self.url())
        self.assertEqual(response.status_code, 401)

    def test_invalid_observations_do_not_create_a_count_or_financial_effect(self):
        for changes in [{"physical_fcfa": True}, {"physical_fcfa": 0.5}, {"physical_fcfa": -1},
                        {"reason": " "}, {"counted_at": "9999-01-01T00:00:00Z"}, {"expected_fcfa": 1000},
                        {"method": "other", "other_name": ""}, {"other_name": "Not cash"}]:
            with self.subTest(changes=changes):
                response = self.client.post(self.url(), self.payload(**changes), format="json")
                self.assertEqual(response.status_code, 400)
        self.assertFalse(CashCount.objects.exists())
        self.assertFalse(CashMovement.objects.exists())

    def test_count_scope_and_uuid_retry_content_are_checked(self):
        data = self.payload(physical_fcfa=0)
        response = self.client.post(self.url(), data, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.data["requires_regularization"])
        other_shop = Shop.objects.create(owner=self.owner, name="B")
        foreign_url = self.url().replace(str(self.shop.id), str(other_shop.id))
        self.assertEqual(self.client.get(foreign_url + data["id"] + "/").status_code, 404)
        self.assertEqual(self.client.get(foreign_url).data["count"], 0)
        data["physical_fcfa"] = 1
        self.assertEqual(self.client.post(self.url(), data, format="json").status_code, 409)
        self.assertEqual(CashCount.objects.get().physical_fcfa, 0)
