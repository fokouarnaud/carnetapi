"""US-23: explicit management of existing money must not invent a collection."""
from datetime import timedelta
import uuid

from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from cash.models import CashMovement
from customers.models import Customer, CustomerDebt, CustomerPayment
from identity.models import DeviceSession, Installation, Membership, Shop, User


class RegularizationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="regular-owner")
        self.seller = User.objects.create_user(username="regular-seller")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        Membership.objects.create(shop=self.shop, user=self.seller)
        self.customer = Customer.objects.create(shop=self.shop, name="Client")
        self.other = Customer.objects.create(shop=self.shop, name="Autre")
        self.device = Installation.objects.create(user=self.owner)
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="test_opening", target_id=self.customer.id, reason="Fixture")
        self.debt = CustomerDebt.objects.create(shop=self.shop, customer=self.customer, actor=self.owner, installation=self.device,
                                               operation=event, amount_fcfa=7000, remaining_fcfa=7000, occurred_at=timezone.now())
        self.client = APIClient()
        self.authenticate(self.owner)
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "parts": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 5000}], "allocations": []}
        response = self.client.post(self.base() + "payments/", data, format="json")
        self.assertEqual(response.status_code, 201)
        self.payment_id = data["id"]
        self.original_payment = response.data

    def authenticate(self, user):
        device = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def base(self, customer=None):
        return f"/api/v1/shops/{self.shop.id}/clients/{(customer or self.customer).id}/"

    def url(self, action, customer=None):
        return self.base(customer) + f"payments/{self.payment_id}/{action}/"

    def allocate_body(self, amount=3000):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Affectation confirmée",
                "allocations": [{"id": str(uuid.uuid4()), "debt_id": str(self.debt.id), "amount_fcfa": amount}]}

    def refund_body(self, amount=2000):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Argent effectivement rendu",
                "amount_fcfa": amount, "method": "cash"}

    def post(self, action, body, customer=None):
        return self.client.post(self.url(action, customer), body, format="json")

    def test_allocation_consumes_existing_money_once_without_second_cash_or_payment(self):
        data = self.allocate_body()
        response = self.post("allocate", data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["kind"], "allocation")
        self.assertEqual(response.data["amount_fcfa"], 3000)
        retry = self.post("allocate", data)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, response.data)
        self.debt.refresh_from_db()
        self.assertEqual(self.debt.remaining_fcfa, 4000)
        payment = CustomerPayment.objects.get(id=self.payment_id)
        self.assertEqual(payment.unallocated_fcfa, 2000)
        self.assertEqual(payment.amount_fcfa, 5000)
        self.assertEqual(CustomerPayment.objects.count(), 1)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 5000)
        audit = Activity.objects.get(id=data["operation_id"])
        self.assertEqual(audit.reason, "Affectation confirmée")
        self.assertEqual(audit.details["before_unallocated_fcfa"], 5000)
        self.assertEqual(audit.details["after_unallocated_fcfa"], 2000)

    def test_real_refund_is_separate_negative_cash_and_does_not_reopen_debt(self):
        self.assertEqual(self.post("allocate", self.allocate_body()).status_code, 201)
        data = self.refund_body()
        response = self.post("refund", data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["kind"], "refund")
        self.assertEqual(self.post("refund", data).data, response.data)
        self.assertEqual(sorted(CashMovement.objects.values_list("amount_fcfa", flat=True)), [-2000, 5000])
        self.assertEqual(CashMovement.objects.aggregate(total=Sum("amount_fcfa"))["total"], 3000)
        payment = CustomerPayment.objects.get(id=self.payment_id)
        self.assertEqual(payment.unallocated_fcfa, 0)
        self.assertFalse(payment.requires_regularization)
        self.assertEqual(payment.amount_fcfa, 5000)
        self.debt.refresh_from_db()
        self.assertEqual(self.debt.remaining_fcfa, 4000)

    def test_seller_cannot_allocate_or_refund_and_other_customer_payment_is_hidden(self):
        self.authenticate(self.seller)
        self.assertEqual(self.post("allocate", self.allocate_body()).status_code, 403)
        self.assertEqual(self.post("refund", self.refund_body()).status_code, 403)
        self.authenticate(self.owner)
        self.assertEqual(self.post("refund", self.refund_body(), self.other).status_code, 404)
        self.assertEqual(CashMovement.objects.count(), 1)

    def test_insufficient_unallocated_or_changed_debt_refuses_decision_without_partial_effect(self):
        self.assertEqual(self.post("refund", self.refund_body(5001)).status_code, 409)
        self.assertEqual(self.post("allocate", self.allocate_body(5001)).status_code, 409)
        self.debt.remaining_fcfa = 1000
        self.debt.save()
        self.assertEqual(self.post("allocate", self.allocate_body()).status_code, 409)
        self.assertEqual(CustomerPayment.objects.get(id=self.payment_id).unallocated_fcfa, 5000)
        self.assertEqual(CashMovement.objects.count(), 1)

    def test_reason_exact_amount_and_operation_content_are_required(self):
        for changes in [{"reason": " "}, {"amount_fcfa": 0}, {"amount_fcfa": True}, {"amount_fcfa": 1.5},
                        {"occurred_at": "2026-01-01T00:00:00Z"}]:
            self.assertEqual(self.post("refund", {**self.refund_body(), **changes}).status_code, 400)
        data = self.refund_body()
        self.assertEqual(self.post("refund", data).status_code, 201)
        self.assertEqual(self.post("refund", {**data, "reason": "Autre motif"}).status_code, 409)
        self.assertEqual(CashMovement.objects.count(), 2)

    def test_regularization_history_preserves_original_payment_and_is_permission_scoped(self):
        self.assertEqual(self.post("allocate", self.allocate_body()).status_code, 201)
        self.assertEqual(self.post("refund", self.refund_body()).status_code, 201)
        response = self.client.get(self.url("regularizations"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual([row["kind"] for row in response.data["results"]], ["allocation", "refund"])
        self.assertEqual([row["reason"] for row in response.data["results"]], ["Affectation confirmée", "Argent effectivement rendu"])
        self.assertEqual(CustomerPayment.objects.get(id=self.payment_id).parts.get().amount_fcfa, 5000)
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.url("regularizations")).status_code, 403)

    def test_foreign_debt_in_later_allocation_rolls_back_first_allocation_and_history(self):
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="test_opening", target_id=self.other.id, reason="Fixture")
        foreign = CustomerDebt.objects.create(shop=self.shop, customer=self.other, actor=self.owner, installation=self.device,
                                              operation=event, amount_fcfa=1000, remaining_fcfa=1000, occurred_at=timezone.now())
        data = self.allocate_body()
        data["allocations"].append({"id": str(uuid.uuid4()), "debt_id": str(foreign.id), "amount_fcfa": 1000})
        self.assertEqual(self.post("allocate", data).status_code, 404)
        self.debt.refresh_from_db()
        self.assertEqual(self.debt.remaining_fcfa, 7000)
        self.assertEqual(CustomerPayment.objects.get(id=self.payment_id).unallocated_fcfa, 5000)
        self.assertEqual(self.client.get(self.url("regularizations")).data["count"], 0)

    def test_retry_rechecks_manager_rights_after_demotion(self):
        manager = User.objects.create_user(username="regular-manager")
        member = Membership.objects.create(shop=self.shop, user=manager, role="manager")
        self.authenticate(manager)
        data = self.refund_body()
        self.assertEqual(self.post("refund", data).status_code, 201)
        member.role = "seller"
        member.save()
        self.assertEqual(self.post("refund", data).status_code, 403)
        self.assertEqual(CashMovement.objects.count(), 2)
