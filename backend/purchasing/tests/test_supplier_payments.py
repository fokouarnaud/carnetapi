"""US-29: a real advance and its later allocation have separate money effects."""
import uuid

from django.test import TestCase

from cash.models import CashMovement
from identity.models import Membership
from stock.models import StockMovement
from purchasing.tests import test_purchases


class SupplierPaymentTests(TestCase):
    setUp = test_purchases.PurchaseTests.setUp
    authenticate = test_purchases.PurchaseTests.authenticate
    url = test_purchases.PurchaseTests.url
    invoice_payload = test_purchases.PurchaseTests.payload

    def payment_payload(self):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-01T00:00:00Z", "backdate_reason": "Reprise historique",
                "reason": "Avance réellement versée", "parts": [
                    {"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 10000},
                    {"id": str(uuid.uuid4()), "method": "orange", "amount_fcfa": 20000}]}

    def invoice(self):
        data = self.invoice_payload()
        data["lines"][0]["quantity_milli"] = 100000
        result = self.client.post(self.url(), data, format="json")
        self.assertEqual(result.status_code, 201)
        return result.data

    def allocation_payload(self, debt_id, amount=30000):
        return {"operation_id": str(uuid.uuid4()), "reason": "Affectation confirmée de l'avance",
                "allocations": [{"debt_id": debt_id, "amount_fcfa": amount}]}

    def test_advance_before_invoice_is_allocated_without_second_cash_outflow(self):
        data = self.payment_payload()
        advance = self.client.post(self.url("payments"), data, format="json")
        self.assertEqual(advance.status_code, 201)
        self.assertEqual(advance.data["unallocated_fcfa"], 30000)
        retry = self.client.post(self.url("payments"), data, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, advance.data)
        invoice = self.invoice()
        self.assertEqual(invoice["debt"]["remaining_fcfa"], 100000)
        url = self.url("payments") + advance.data["id"] + "/allocate/"
        allocation = self.allocation_payload(invoice["debt"]["id"])
        result = self.client.post(url, allocation, format="json")
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.data["after_unallocated_fcfa"], 0)
        self.assertEqual(self.client.post(url, allocation, format="json").data, result.data)
        current = self.client.get(self.url() + invoice["id"] + "/")
        self.assertEqual(current.data["debt"]["remaining_fcfa"], 70000)
        current_payment = self.client.get(self.url("payments") + advance.data["id"] + "/")
        self.assertEqual(current_payment.data["amount_fcfa"], 30000)
        self.assertEqual(current_payment.data["unallocated_fcfa"], 0)
        self.assertEqual(current_payment.data["allocations"][0]["reason"], allocation["reason"])
        self.assertEqual(current_payment.data["allocations"][0]["actor_id"], str(self.owner.id))
        self.assertEqual(CashMovement.objects.count(), 2)
        self.assertEqual(sorted(CashMovement.objects.values_list("amount_fcfa", flat=True)), [-20000, -10000])
        self.assertFalse(StockMovement.objects.exists())
        cash = self.client.get(f"/api/v1/shops/{self.shop.id}/cash/")
        self.assertEqual(cash.data["totals_by_method_fcfa"], {"cash": -10000, "orange": -20000})
        self.assertEqual({row["kind"] for row in cash.data["results"]}, {"supplier_payment"})

    def test_seller_cannot_pay_or_allocate_even_with_cost_and_cash_permissions(self):
        Membership.objects.filter(user=self.seller).update(can_read_cost=True, can_read_cash_balance=True)
        self.authenticate(self.seller)
        self.assertEqual(self.client.post(self.url("payments"), self.payment_payload(), format="json").status_code, 403)
        self.assertEqual(self.client.get(self.url("payments")).status_code, 403)
        self.assertFalse(CashMovement.objects.exists())

    def test_allocating_more_than_advance_keeps_debt_and_money_unchanged(self):
        advance = self.client.post(self.url("payments"), self.payment_payload(), format="json")
        self.assertEqual(advance.status_code, 201)
        invoice = self.invoice()
        url = self.url("payments") + advance.data["id"] + "/allocate/"
        result = self.client.post(url, self.allocation_payload(invoice["debt"]["id"], 30001), format="json")
        self.assertEqual(result.status_code, 400)
        self.assertEqual(self.client.get(self.url() + invoice["id"] + "/").data["debt"]["remaining_fcfa"], 100000)
        self.assertEqual(self.client.get(self.url("payments") + advance.data["id"] + "/").data["unallocated_fcfa"], 30000)
        self.assertEqual(CashMovement.objects.count(), 2)

    def test_changed_payment_retry_cannot_change_actual_cash(self):
        data = self.payment_payload()
        self.assertEqual(self.client.post(self.url("payments"), data, format="json").status_code, 201)
        data["parts"][0]["amount_fcfa"] = 9000
        self.assertEqual(self.client.post(self.url("payments"), data, format="json").status_code, 409)
        self.assertEqual(sum(CashMovement.objects.values_list("amount_fcfa", flat=True)), -30000)

    def test_foreign_supplier_debt_in_second_allocation_has_no_partial_effect(self):
        from purchasing.models import SupplierDebt
        advance = self.client.post(self.url("payments"), self.payment_payload(), format="json")
        self.assertEqual(advance.status_code, 201)
        invoice = self.invoice()
        supplier = self.client.post(f"/api/v1/shops/{self.shop.id}/suppliers/",
                                    {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Autre"}, format="json")
        self.assertEqual(supplier.status_code, 201)
        foreign = self.client.post(f"/api/v1/shops/{self.shop.id}/suppliers/{supplier.data['id']}/debts/",
                                  {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 10000,
                                   "occurred_at": "2026-01-01T00:00:00Z", "reason": "Ouverture"}, format="json")
        self.assertEqual(foreign.status_code, 201)
        data = self.allocation_payload(invoice["debt"]["id"], 10000)
        data["allocations"].append({"debt_id": foreign.data["id"], "amount_fcfa": 10000})
        url = self.url("payments") + advance.data["id"] + "/allocate/"
        self.assertEqual(self.client.post(url, data, format="json").status_code, 404)
        self.assertEqual(SupplierDebt.objects.get(id=invoice["debt"]["id"]).remaining_fcfa, 100000)
        self.assertEqual(self.client.get(self.url("payments") + advance.data["id"] + "/").data["unallocated_fcfa"], 30000)

    def test_named_payment_is_one_outflow_in_ledger_and_physical_count(self):
        data = self.payment_payload()
        data["parts"] = [{"id": str(uuid.uuid4()), "method": "other", "other_name": "Compte local", "external_reference": "REF-A", "amount_fcfa": 30000}]
        result = self.client.post(self.url("payments"), data, format="json")
        self.assertEqual(result.status_code, 201)
        cash = self.client.get(f"/api/v1/shops/{self.shop.id}/cash/")
        self.assertEqual(cash.data["totals_by_method_fcfa"], {"other:Compte local": -30000})
        self.assertEqual(cash.data["results"][0]["external_reference"], "REF-A")
        count = self.client.post(f"/api/v1/shops/{self.shop.id}/cash-counts/",
                                 {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "method": "other",
                                  "other_name": "Compte local", "physical_fcfa": 0, "counted_at": "2026-01-02T00:00:00Z", "reason": "Comptage"}, format="json")
        self.assertEqual(count.status_code, 201)
        self.assertEqual(count.data["expected_fcfa"], -30000)
        self.assertEqual(count.data["difference_fcfa"], 30000)

    def test_database_rejects_positive_supplier_outflow_and_two_money_sources(self):
        from django.db import IntegrityError, transaction
        result = self.client.post(self.url("payments"), self.payment_payload(), format="json")
        self.assertEqual(result.status_code, 201)
        movement = CashMovement.objects.first()
        for changes in ({"amount_fcfa": 10000}, {"manual_kind": "expense"}, {"supplier_payment_part_id": None}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                CashMovement.objects.filter(id=movement.id).update(**changes)

    def test_database_rejects_unknown_or_unnamed_supplier_payment_method(self):
        from django.db import IntegrityError, transaction
        from purchasing.models import SupplierPaymentPart
        result = self.client.post(self.url("payments"), self.payment_payload(), format="json")
        self.assertEqual(result.status_code, 201)
        part = SupplierPaymentPart.objects.first()
        for changes in ({"method": "unknown"}, {"method": "other", "other_name": ""}, {"method": "cash", "other_name": "Faux compte"}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                SupplierPaymentPart.objects.filter(id=part.id).update(**changes)
