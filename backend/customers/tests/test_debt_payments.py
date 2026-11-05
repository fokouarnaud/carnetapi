"""US-21/23: one collection, explicit allocations and preserved overpayments."""
from datetime import timedelta
import uuid

from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from cash.models import CashMovement
from customers.models import Customer, CustomerDebt
from identity.models import DeviceSession, Installation, Membership, Shop, User


class DebtPaymentTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="collection-owner")
        self.seller = User.objects.create_user(username="collection-seller")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        self.member = Membership.objects.create(shop=self.shop, user=self.seller)
        self.customer = Customer.objects.create(shop=self.shop, name="Client")
        self.other_customer = Customer.objects.create(shop=self.shop, name="Autre client")
        self.device = Installation.objects.create(user=self.owner)
        self.first = self.debt(3000, self.customer, timezone.now() - timedelta(days=2))
        self.second = self.debt(7000, self.customer, timezone.now() - timedelta(days=1))
        self.foreign_debt = self.debt(1000, self.other_customer, timezone.now())
        self.client = APIClient()
        self.authenticate(self.seller)

    def debt(self, amount, customer, at):
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="test_opening_debt", target_id=customer.id, reason="Fixture")
        return CustomerDebt.objects.create(shop=self.shop, customer=customer, actor=self.owner, installation=self.device,
                                          operation=event, amount_fcfa=amount, remaining_fcfa=amount, occurred_at=at)

    def authenticate(self, user):
        device = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def url(self, suffix="payments/"):
        return f"/api/v1/shops/{self.shop.id}/clients/{self.customer.id}/{suffix}"

    def payload(self, **changes):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "parts": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 5000}],
                "allocations": [{"id": str(uuid.uuid4()), "debt_id": str(self.first.id), "amount_fcfa": 3000},
                                {"id": str(uuid.uuid4()), "debt_id": str(self.second.id), "amount_fcfa": 2000}], **changes}

    def create(self, data=None):
        return self.client.post(self.url(), data or self.payload(), format="json")

    def test_oldest_first_preview_is_read_only_and_sums_the_whole_customer_filter(self):
        response = self.client.get(self.url("payment-preview/"), {"amount_fcfa": 5000})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["known_debt_fcfa"], 10000)
        self.assertEqual(response.data["allocations"], [
            {"debt_id": str(self.first.id), "amount_fcfa": 3000},
            {"debt_id": str(self.second.id), "amount_fcfa": 2000},
        ])
        self.assertEqual(response.data["unallocated_fcfa"], 0)
        self.assertFalse(CashMovement.objects.exists())

    def test_collection_without_credit_permission_allocates_3000_plus_2000_and_cash_once(self):
        data = self.payload()
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["amount_fcfa"], 5000)
        self.assertEqual(response.data["unallocated_fcfa"], 0)
        self.assertEqual([item["applied_amount_fcfa"] for item in response.data["allocations"]], [3000, 2000])
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual(self.first.remaining_fcfa, 0)
        self.assertEqual(self.second.remaining_fcfa, 5000)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 5000)
        self.assertEqual(self.create(data).status_code, 200)
        self.assertEqual(CashMovement.objects.count(), 1)
        self.assertEqual(self.client.get(self.url()).data["count"], 1)
        self.assertEqual(self.client.get(self.url("debts/")).data["count"], 2)

    def test_second_real_collection_is_preserved_and_excess_does_not_pay_other_debt_automatically(self):
        def body():
            return self.payload(parts=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 3000}],
                                allocations=[{"id": str(uuid.uuid4()), "debt_id": str(self.first.id), "amount_fcfa": 3000}])
        self.assertEqual(self.create(body()).status_code, 201)
        response = self.create(body())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["unallocated_fcfa"], 3000)
        self.assertTrue(response.data["requires_regularization"])
        self.assertEqual(response.data["allocations"][0]["requested_amount_fcfa"], 3000)
        self.assertEqual(response.data["allocations"][0]["applied_amount_fcfa"], 0)
        self.second.refresh_from_db()
        self.assertEqual(self.second.remaining_fcfa, 7000)
        self.assertEqual(CashMovement.objects.aggregate(total=Sum("amount_fcfa"))["total"], 6000)
        self.assertEqual(self.client.get(self.url()).data["count"], 2)

    def test_means_are_ventilated_and_uuid_conflict_does_not_create_second_cash(self):
        data = self.payload(parts=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 2000},
                                   {"id": str(uuid.uuid4()), "method": "orange", "amount_fcfa": 3000}])
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(CashMovement.objects.get(method="cash").amount_fcfa, 2000)
        self.assertEqual(CashMovement.objects.get(method="orange").amount_fcfa, 3000)
        self.assertEqual(self.create({**data, "allocations": []}).status_code, 409)
        self.assertEqual(CashMovement.objects.count(), 2)

    def test_allocation_to_other_customer_rolls_back_entire_payment_and_debt_changes(self):
        data = self.payload()
        data["allocations"][1]["debt_id"] = str(self.foreign_debt.id)
        self.assertEqual(self.create(data).status_code, 404)
        self.first.refresh_from_db()
        self.assertEqual(self.first.remaining_fcfa, 3000)
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(Activity.objects.filter(action="customer_payment_created").exists())

    def test_permission_is_checked_for_collection_preview_reads_and_retry(self):
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        self.member.can_collect_debt = False
        self.member.save()
        self.assertEqual(self.create(data).status_code, 403)
        self.assertEqual(self.client.get(self.url("payment-preview/"), {"amount_fcfa": 1000}).status_code, 403)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.member.can_read_debt = True
        self.member.save()
        self.assertEqual(self.client.get(self.url()).status_code, 200)
        self.assertEqual(self.create().status_code, 403)

    def test_invalid_totals_duplicate_ids_and_coerced_amounts_have_no_effect(self):
        invalid = [self.payload(parts=[]), self.payload(occurred_at="2026-01-01T00:00:00Z")]
        for amount in [0, -1, True, 1.5, "5000"]:
            invalid.append(self.payload(parts=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": amount}]))
        data = self.payload()
        data["allocations"][1]["amount_fcfa"] = 3000
        invalid.append(data)
        data = self.payload()
        data["allocations"][1]["id"] = data["allocations"][0]["id"]
        invalid.append(data)
        for data in invalid:
            with self.subTest(data=data):
                self.assertEqual(self.create(data).status_code, 400)
        self.assertFalse(CashMovement.objects.exists())
        self.first.refresh_from_db()
        self.assertEqual(self.first.remaining_fcfa, 3000)
