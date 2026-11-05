"""US-23/26: return credit is refunded or allocated only by an explicit decision."""
import uuid
from django.test import TestCase
from django.utils import timezone
from sales.tests import test_sale_returns as return_tests
from cash.models import CashMovement
from customers.models import Customer, CustomerDebt, ReturnCreditSettlement
from identity.models import Membership
from stock.models import ReturnReceipt, StockMovement
from sales.models import Sale, SalePayment


class ReturnCreditSettlementTests(TestCase):
    setUp = return_tests.SaleReturnTests.setUp
    authenticate = return_tests.SaleReturnTests.authenticate
    payload = return_tests.SaleReturnTests.payload
    url = return_tests.SaleReturnTests.url
    prepare_sale = return_tests.SaleReturnTests.prepare_sale
    return_url = return_tests.SaleReturnTests.return_url
    return_payload = return_tests.SaleReturnTests.return_payload

    def prepare_credit(self, identified=False):
        customer = Customer.objects.create(shop=self.shop, name="Client") if identified else None
        extra = {"mode": "credit", "customer_id": str(customer.id), "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 4000}]} if customer else {}
        sale = self.prepare_sale(**extra)
        data = self.return_payload(sale, 2000)
        response = self.client.post(self.return_url(sale), data, format="json")
        self.assertEqual(response.status_code, 201)
        return sale, response.data, customer, data

    def base(self, sale, returned):
        return self.return_url(sale) + returned["id"] + "/"

    def refund(self, amount=3000, **extra):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
            "reason": "Argent effectivement rendu", "amount_fcfa": amount, "method": "cash", **extra}

    def test_anonymous_refund_is_one_real_cash_outflow_with_current_balance_and_original_retry_receipt(self):
        sale, returned, _, original_data = self.prepare_credit()
        data = self.refund()
        path = self.base(sale, returned) + "refunds/"
        refunded = self.client.post(path, data, format="json")
        self.assertEqual(refunded.status_code, 201, refunded.content)
        self.assertEqual(refunded.data["after_credit_fcfa"], 7000)
        self.assertEqual(self.client.post(path, data, format="json").data, refunded.data)
        self.assertEqual(CashMovement.objects.get(sale_return_refund_id=data["id"]).amount_fcfa, -3000)
        self.assertEqual(CashMovement.objects.count(), 3)
        self.assertEqual(SalePayment.objects.count(), 2)
        self.assertFalse(Customer.objects.exists())
        self.assertEqual(self.client.get(self.base(sale, returned)).data["remaining_credit_fcfa"], 7000)
        self.assertEqual(self.client.post(self.return_url(sale), original_data, format="json").data["remaining_credit_fcfa"], 10000)
        self.assertEqual(self.client.post(path, self.refund(7001), format="json").status_code, 409)
        self.assertEqual(ReturnCreditSettlement.objects.count(), 1)
        self.assertEqual(self.client.get(path).data["count"], 1)

    def test_identified_allocation_then_refund_preserves_return_old_payments_and_revises_statement(self):
        sale, returned, customer, _ = self.prepare_credit(True)
        new_sale = self.prepare_sale(mode="credit", customer_id=str(customer.id), payments=[])
        debt = CustomerDebt.objects.get(sale_id=new_sale["id"])
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
            "reason": "Affectation confirmée", "allocations": [{"id": str(uuid.uuid4()), "debt_id": str(debt.id), "amount_fcfa": 3000}]}
        path = self.base(sale, returned) + "allocations/"
        allocation = self.client.post(path, data, format="json")
        self.assertEqual(allocation.status_code, 201, allocation.content)
        self.assertEqual(allocation.data["after_credit_fcfa"], 1000)
        self.assertEqual(self.client.post(path, data, format="json").data, allocation.data)
        debt.refresh_from_db()
        self.assertEqual(debt.remaining_fcfa, 7000)
        self.assertEqual(CashMovement.objects.count(), 1)
        result = self.client.post(self.base(sale, returned) + "refunds/", self.refund(1000), format="json")
        self.assertEqual(result.status_code, 201)
        statement = self.client.get(f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/statement/").data
        self.assertEqual(statement["closing_balance_fcfa"], 7000)
        self.assertEqual(statement["closing_debt_fcfa"], 7000)
        self.assertEqual(statement["closing_unallocated_fcfa"], 0)
        row = next(row for row in statement["results"] if row["kind"] == "return_credit_allocation")
        self.assertEqual(row["allocations"][0]["debt_id"], str(debt.id))
        self.assertEqual(row["allocations"][0]["applied_amount_fcfa"], 3000)
        credits = self.client.get(f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/return-credits/")
        self.assertEqual(credits.status_code, 200)
        self.assertEqual(credits.data["results"][0]["remaining_fcfa"], 0)
        self.assertEqual(Sale.objects.get(id=sale["id"]).net_total_fcfa, 10000)
        self.assertEqual(SalePayment.objects.get().amount_fcfa, 4000)
        self.assertEqual(StockMovement.objects.filter(kind="sale").count(), 2)
        self.assertFalse(ReturnReceipt.objects.exists())

    def test_other_refund_method_is_preserved_in_cash_ledger_and_cash_count(self):
        from cash.api.cash_counts import expected_cash_at
        sale, returned, _, _ = self.prepare_credit()
        data = self.refund(2000, method="other", other_name="Carte locale", external_reference="REF-7")
        response = self.client.post(self.base(sale, returned) + "refunds/", data, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        cash = self.client.get(f"/api/v1/shops/{self.shop.id}/cash/").data
        row = next(row for row in cash["results"] if row["kind"] == "sale_return_refund")
        self.assertEqual(row["other_name"], "Carte locale")
        self.assertEqual(row["external_reference"], "REF-7")
        self.assertEqual(cash["totals_by_method_fcfa"]["other:Carte locale"], -2000)
        self.assertEqual(expected_cash_at(self.shop.id, "other", "Carte locale", timezone.now()), -2000)

    def test_seller_foreign_parent_bad_input_and_changed_replay_are_refused(self):
        sale, returned, _, _ = self.prepare_credit()
        path = self.base(sale, returned) + "refunds/"
        data = self.refund()
        self.authenticate(self.seller)
        self.assertEqual(self.client.post(path, data, format="json").status_code, 403)
        self.assertEqual(self.client.get(path).status_code, 403)
        self.authenticate(self.owner)
        self.assertEqual(self.client.post(path.replace(str(self.shop.id), str(self.other.id)), data, format="json").status_code, 404)
        for changes in ({"amount_fcfa": 0}, {"amount_fcfa": 1.5}, {"reason": " "}, {"method": "other"}, {"occurred_at": "2026-01-01T00:00:00Z"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.client.post(path, {**data, **changes}, format="json").status_code, 400)
        self.assertEqual(self.client.post(path, data, format="json").status_code, 201)
        self.assertEqual(self.client.post(path, {**data, "amount_fcfa": 2000}, format="json").status_code, 409)
        self.assertEqual(ReturnCreditSettlement.objects.count(), 1)

    def test_foreign_allocation_rolls_back_and_repeated_debt_is_refused(self):
        sale, returned, customer, _ = self.prepare_credit(True)
        new_sale = self.prepare_sale(mode="credit", customer_id=str(customer.id), payments=[])
        debt = CustomerDebt.objects.get(sale_id=new_sale["id"])
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
            "reason": "Affectation contrôlée", "allocations": [{"id": str(uuid.uuid4()), "debt_id": str(debt.id), "amount_fcfa": 1000}]}
        bad = {**data, "allocations": data["allocations"] + [{"id": str(uuid.uuid4()), "debt_id": str(uuid.uuid4()), "amount_fcfa": 1000}]}
        path = self.base(sale, returned) + "allocations/"
        self.assertEqual(self.client.post(path, bad, format="json").status_code, 404)
        repeated = {**data, "allocations": data["allocations"] + [{**data["allocations"][0], "id": str(uuid.uuid4())}]}
        self.assertEqual(self.client.post(path, repeated, format="json").status_code, 400)
        self.assertFalse(ReturnCreditSettlement.objects.exists())
        debt.refresh_from_db()
        self.assertEqual(debt.remaining_fcfa, 10000)
        self.assertEqual(CashMovement.objects.count(), 1)

    def test_manager_demotion_blocks_old_refund_receipt_and_credit_reader_uses_debt_permission(self):
        sale, returned, customer, _ = self.prepare_credit(True)
        self.member.role = Membership.Role.MANAGER
        self.member.save(update_fields=["role"])
        self.authenticate(self.seller)
        data = self.refund(1000)
        path = self.base(sale, returned) + "refunds/"
        self.assertEqual(self.client.post(path, data, format="json").status_code, 201)
        self.member.role = Membership.Role.SELLER
        self.member.can_collect_debt = False
        self.member.save(update_fields=["role", "can_collect_debt"])
        self.assertEqual(self.client.post(path, data, format="json").status_code, 403)
        credits_path = f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/return-credits/"
        self.assertEqual(self.client.get(credits_path).status_code, 403)
        self.member.can_read_debt = True
        self.member.save(update_fields=["can_read_debt"])
        credits = self.client.get(credits_path)
        self.assertEqual(credits.status_code, 200)
        self.assertEqual(credits.data["results"][0]["remaining_fcfa"], 3000)
        self.assertNotIn("reason", credits.data["results"][0])
        self.assertEqual(self.client.post(path, self.refund(1000), format="json").status_code, 403)

    def test_conflict_is_translated_in_english(self):
        sale, returned, _, _ = self.prepare_credit()
        response = self.client.post(self.base(sale, returned) + "refunds/", self.refund(10001), format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "return_credit_changed")
        self.assertEqual(response.json()["detail"], "Return credit or debt has changed. Read it again before confirming.")

    def test_full_audit_reason_remains_valid_in_the_cash_data_model(self):
        sale, returned, _, _ = self.prepare_credit()
        reason = "R" * 2000
        data = self.refund(1000, reason=reason)
        response = self.client.post(self.base(sale, returned) + "refunds/", data, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        movement = CashMovement.objects.get(sale_return_refund_id=data["id"])
        self.assertEqual(movement.reason, reason)
        movement.full_clean()

    def test_allocation_cannot_be_effective_before_the_target_debt(self):
        sale, returned, customer, _ = self.prepare_credit(True)
        new_sale = self.prepare_sale(mode="credit", customer_id=str(customer.id), payments=[])
        debt = CustomerDebt.objects.get(sale_id=new_sale["id"])
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": returned["occurred_at"],
            "reason": "Date antérieure à la dette", "allocations": [{"id": str(uuid.uuid4()), "debt_id": str(debt.id), "amount_fcfa": 1000}]}
        response = self.client.post(self.base(sale, returned) + "allocations/", data, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(ReturnCreditSettlement.objects.exists())
        debt.refresh_from_db()
        self.assertEqual(debt.remaining_fcfa, 10000)
