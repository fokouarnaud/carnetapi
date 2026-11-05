"""US-22: handed money, actual change and unpaid customer obligations."""
from datetime import timedelta
import uuid

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from cash.models import CashMovement
from customers.models import Customer, CustomerCredit, CustomerDebt, CustomerPaymentPart, PaymentRegularization
from identity.models import DeviceSession, Installation, Membership, Shop, User
from catalogue.models import Product
from sales.models import SalePayment


class ChangeDueTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="change-owner")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        self.customer = Customer.objects.create(shop=self.shop, name="Client monnaie")
        self.product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=7500)
        device = Installation.objects.create(user=self.owner)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=30))
        access = AccessToken.for_user(self.owner)
        access["session_id"] = str(session.id)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def payload(self, retained=7500, returned=2500, **changes):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1, "quantity_milli": 1000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": retained,
                              "received_fcfa": 10000, "returned_fcfa": returned}], **changes}

    def create(self, data):
        return self.client.post(f"/api/v1/shops/{self.shop.id}/sales/", data, format="json")

    def test_real_change_gives_7500_cash_and_preserves_handed_10000_returned_2500(self):
        data = self.payload()
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["net_total_fcfa"], 7500)
        self.assertEqual(response.data["payments"][0]["received_fcfa"], 10000)
        self.assertEqual(response.data["payments"][0]["returned_fcfa"], 2500)
        self.assertIsNone(response.data["customer_credit"])
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 7500)
        self.assertEqual(self.create(data).data, response.data)
        self.assertEqual(CashMovement.objects.count(), 1)

    def test_no_change_requires_client_and_creates_2500_obligation_without_extra_revenue(self):
        data = self.payload(retained=10000, returned=0, customer_id=str(self.customer.id))
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["net_total_fcfa"], 7500)
        self.assertEqual(response.data["customer_credit"]["amount_fcfa"], 2500)
        self.assertEqual(response.data["customer_credit"]["remaining_fcfa"], 2500)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 10000)
        self.assertEqual(self.create(data).status_code, 200)
        statement = self.client.get(f"/api/v1/shops/{self.shop.id}/clients/{self.customer.id}/statement/")
        self.assertEqual(statement.status_code, 200)
        self.assertEqual(statement.data["closing_balance_fcfa"], -2500)
        self.assertEqual(statement.data["closing_debt_fcfa"], 0)
        self.assertEqual(statement.data["closing_unallocated_fcfa"], 2500)

    def test_overpayment_without_client_and_inconsistent_handed_or_change_are_rejected(self):
        self.assertEqual(self.create(self.payload(10000, 0)).status_code, 400)
        for fields in [{"amount_fcfa": 7500, "received_fcfa": 10000, "returned_fcfa": 2000},
                       {"received_fcfa": True}, {"returned_fcfa": -1}, {"received_fcfa": 10000.5}]:
            data = self.payload()
            data["payments"][0].update(fields)
            self.assertEqual(self.create(data).status_code, 400)
        self.assertFalse(CashMovement.objects.exists())

    def test_external_reference_duplicate_is_flagged_but_second_real_sale_is_preserved(self):
        def data():
            return self.payload(payments=[{"id": str(uuid.uuid4()), "method": "orange", "amount_fcfa": 7500,
                                           "external_reference": "ORANGE-DECLARED-1"}])
        first = self.create(data())
        self.assertEqual(first.status_code, 201)
        self.assertFalse(first.data["payments"][0]["possible_duplicate_reference"])
        second = self.create(data())
        self.assertEqual(second.status_code, 201)
        self.assertTrue(second.data["payments"][0]["possible_duplicate_reference"])
        self.assertEqual(CashMovement.objects.aggregate(total=Sum("amount_fcfa"))["total"], 15000)

    def test_database_rejects_partial_or_inconsistent_change_metadata(self):
        self.assertEqual(self.create(self.payload()).status_code, 201)
        for fields in [{"received_fcfa": None}, {"returned_fcfa": None}, {"returned_fcfa": 2000}]:
            with self.subTest(fields=fields):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    SalePayment.objects.update(**fields)
        payment = SalePayment.objects.get()
        self.assertEqual(payment.received_fcfa - payment.returned_fcfa, payment.amount_fcfa)

    def test_legacy_declared_net_preserves_unknown_handed_and_returned_amounts(self):
        data = self.payload(payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 7500}])
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data["payments"][0]["received_fcfa"])
        self.assertIsNone(response.data["payments"][0]["returned_fcfa"])

    def test_customer_obligation_cannot_be_downloaded_without_debt_permission(self):
        data = self.payload(10000, 0, customer_id=str(self.customer.id))
        self.assertEqual(self.create(data).status_code, 201)
        seller = User.objects.create_user(username="change-seller")
        member = Membership.objects.create(shop=self.shop, user=seller, role="seller", can_collect_debt=False)
        installation = Installation.objects.create(user=seller)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        access = AccessToken.for_user(seller)
        access["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        url = f"/api/v1/shops/{self.shop.id}/sales/"
        self.assertEqual(self.client.get(url).data["count"], 0)
        self.assertEqual(self.client.get(url + data["id"] + "/").status_code, 404)
        member.can_read_debt = True
        member.save()
        self.assertEqual(self.client.get(url).data["count"], 1)

    def test_customer_credit_list_is_scoped_and_exposes_unpaid_change_separately(self):
        response = self.create(self.payload(10000, 0, customer_id=str(self.customer.id)))
        self.assertEqual(response.status_code, 201)
        url = f"/api/v1/shops/{self.shop.id}/clients/{self.customer.id}/credits/"
        credits = self.client.get(url)
        self.assertEqual(credits.status_code, 200)
        self.assertEqual(credits.data["count"], 1)
        self.assertEqual(credits.data["results"][0], response.data["customer_credit"])
        other = Customer.objects.create(shop=self.shop, name="Autre")
        self.assertEqual(self.client.get(url.replace(str(self.customer.id), str(other.id))).data["count"], 0)
        other_shop = Shop.objects.create(owner=self.owner, name="B")
        self.assertEqual(self.client.get(url.replace(str(self.shop.id), str(other_shop.id))).status_code, 404)

    def credit_url(self, suffix):
        response = self.create(self.payload(10000, 0, customer_id=str(self.customer.id)))
        self.assertEqual(response.status_code, 201)
        return f"/api/v1/shops/{self.shop.id}/clients/{self.customer.id}/credits/{response.data['customer_credit']['id']}/{suffix}/"

    def test_actual_refund_reduces_customer_obligation_and_cash_once(self):
        url = self.credit_url("refund")
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Monnaie réellement rendue",
                "method": "cash", "amount_fcfa": 1000}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["after_unallocated_fcfa"], 1500)
        self.assertEqual(CashMovement.objects.aggregate(total=Sum("amount_fcfa"))["total"], 9000)
        credit = CustomerCredit.objects.get()
        self.assertEqual(credit.amount_fcfa, 2500)
        self.assertEqual(credit.remaining_fcfa, 1500)
        self.assertEqual(self.client.post(url, data, format="json").data, response.data)
        self.assertEqual(CashMovement.objects.count(), 2)
        data.update(id=str(uuid.uuid4()), operation_id=str(uuid.uuid4()), amount_fcfa=2000)
        self.assertEqual(self.client.post(url, data, format="json").status_code, 409)
        statement = self.client.get(f"/api/v1/shops/{self.shop.id}/clients/{self.customer.id}/statement/").data
        self.assertEqual(statement["closing_balance_fcfa"], -1500)
        self.assertEqual(statement["closing_unallocated_fcfa"], 1500)

    def test_explicit_credit_allocation_pays_debt_without_new_cash(self):
        opening = self.client.post(f"/api/v1/shops/{self.shop.id}/clients/{self.customer.id}/debts/",
                                   {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 3000,
                                    "occurred_at": timezone.now().isoformat(), "reason": "Solde antérieur confirmé"}, format="json")
        self.assertEqual(opening.status_code, 201)
        debt = CustomerDebt.objects.get(id=opening.data["id"])
        url = self.credit_url("allocate")
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Affectation confirmée",
                "allocations": [{"id": str(uuid.uuid4()), "debt_id": str(debt.id), "amount_fcfa": 2000}]}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["after_unallocated_fcfa"], 500)
        debt.refresh_from_db()
        self.assertEqual(debt.remaining_fcfa, 1000)
        self.assertEqual(CashMovement.objects.count(), 1)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 10000)
        self.assertEqual(self.client.post(url, data, format="json").data, response.data)
        history = self.client.get(url.replace("/allocate/", "/regularizations/"))
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.data["count"], 1)
        statement = self.client.get(f"/api/v1/shops/{self.shop.id}/clients/{self.customer.id}/statement/")
        decision = next(row for row in statement.data["results"] if row["kind"] == "allocation")
        self.assertEqual(decision["allocations"][0]["applied_amount_fcfa"], 2000)
        self.assertEqual(statement.data["closing_balance_fcfa"], 500)
        self.assertEqual(statement.data["closing_debt_fcfa"], 1000)
        self.assertEqual(statement.data["closing_unallocated_fcfa"], 500)

    def test_collections_keep_real_change_and_reference_warning_is_scoped_and_frozen(self):
        def payload():
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "allocations": [],
                    "parts": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 7500,
                               "received_fcfa": 10000, "returned_fcfa": 2500, "external_reference": "same-ref"}]}
        other_shop = Shop.objects.create(owner=self.owner, name="B")
        other = Customer.objects.create(shop=other_shop, name="Autre")
        url = f"/api/v1/shops/{self.shop.id}/clients/{self.customer.id}/payments/"
        foreign_url = f"/api/v1/shops/{other_shop.id}/clients/{other.id}/payments/"
        self.assertEqual(self.client.post(foreign_url, payload(), format="json").status_code, 201)
        data = self.payload(payments=payload()["parts"])
        first = self.create(data)
        self.assertEqual(first.status_code, 201)
        self.assertFalse(first.data["payments"][0]["possible_duplicate_reference"])
        response = self.client.post(url, payload(), format="json")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["parts"][0]["possible_duplicate_reference"])
        self.assertEqual(response.data["parts"][0]["received_fcfa"], 10000)
        self.assertEqual(response.data["parts"][0]["returned_fcfa"], 2500)
        self.assertEqual(response.data["amount_fcfa"], 7500)
        self.assertEqual(CashMovement.objects.filter(shop=self.shop).aggregate(total=Sum("amount_fcfa"))["total"], 15000)
        self.assertEqual(self.create(data).data, first.data)
        for fields in [{"received_fcfa": None}, {"returned_fcfa": 0}]:
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), transaction.atomic():
                CustomerPaymentPart.objects.filter(payment_id=response.data["id"]).update(**fields)

    def test_seller_cannot_regularize_credit_even_with_debt_read_permission(self):
        url = self.credit_url("refund")
        seller = User.objects.create_user(username="refund-seller")
        member = Membership.objects.create(shop=self.shop, user=seller, role="seller", can_read_debt=True)
        installation = Installation.objects.create(user=seller)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        access = AccessToken.for_user(seller)
        access["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        credits_url = f"/api/v1/shops/{self.shop.id}/clients/{self.customer.id}/credits/"
        self.assertEqual(self.client.get(credits_url).status_code, 200)
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Rendu", "method": "cash", "amount_fcfa": 1000}
        self.assertEqual(self.client.post(url, data, format="json").status_code, 403)
        self.assertEqual(self.client.get(url.replace("/refund/", "/regularizations/")).status_code, 403)
        member.can_read_debt = False
        member.can_collect_debt = False
        member.save()
        self.assertEqual(self.client.get(credits_url).status_code, 403)
        self.assertEqual(CashMovement.objects.count(), 1)
        self.assertEqual(CustomerCredit.objects.get().remaining_fcfa, 2500)

    def test_foreign_allocation_after_valid_row_rolls_back_credit_and_debt(self):
        other = Customer.objects.create(shop=self.shop, name="Autre")
        def opening(customer):
            response = self.client.post(f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/debts/",
                                        {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 1000,
                                         "occurred_at": timezone.now().isoformat(), "reason": "Solde initial"}, format="json")
            self.assertEqual(response.status_code, 201)
            return response.data["id"]
        first_id, foreign_id = opening(self.customer), opening(other)
        url = self.credit_url("allocate")
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Affectation",
                "allocations": [{"id": str(uuid.uuid4()), "debt_id": first_id, "amount_fcfa": 500},
                                {"id": str(uuid.uuid4()), "debt_id": foreign_id, "amount_fcfa": 500}]}
        self.assertEqual(self.client.post(url, data, format="json").status_code, 404)
        self.assertEqual(CustomerDebt.objects.get(id=first_id).remaining_fcfa, 1000)
        self.assertEqual(CustomerCredit.objects.get().remaining_fcfa, 2500)
        self.assertFalse(PaymentRegularization.objects.exists())
        self.assertEqual(CashMovement.objects.count(), 1)
