"""US-30: an accepted credit reduces origin debt; only real refunds add cash."""
import uuid
from django.test import TestCase
from cash.models import CashMovement
from purchasing.models import SupplierDebt
from stock.models import StockMovement
from purchasing.tests import test_supplier_returns


class SupplierCreditTests(TestCase):
    setUp = test_supplier_returns.SupplierReturnTests.setUp
    authenticate = test_supplier_returns.SupplierReturnTests.authenticate
    url = test_supplier_returns.SupplierReturnTests.url
    invoice_payload = test_supplier_returns.SupplierReturnTests.invoice_payload
    receipt_payload = test_supplier_returns.SupplierReturnTests.receipt_payload
    invoice = test_supplier_returns.SupplierReturnTests.invoice
    return_payload = test_supplier_returns.SupplierReturnTests.return_payload

    def departure(self):
        invoice = self.invoice()
        response = self.client.post(self.url() + invoice["id"] + "/returns/", self.return_payload(invoice), format="json")
        self.assertEqual(response.status_code, 201)
        return invoice, response.data

    def credit_payload(self, departure, amount=5000):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "return_id": departure["id"],
                "occurred_at": "2026-01-04T00:00:00Z", "amount_fcfa": amount, "reason": "Avoir réellement accepté"}

    def test_accepted_credit_settles_origin_debt_and_keeps_excess_without_cash_or_other_debt_allocation(self):
        invoice, departure = self.departure()
        # A real prior payment left 3000 owed on the original invoice.
        paid = self.client.post(self.url("payments"), {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-03T00:00:00Z", "backdate_reason": "Reprise historique",
            "reason": "Paiement réel", "parts": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 7000}],
        }, format="json")
        self.assertEqual(paid.status_code, 201)
        allocation = self.client.post(self.url("payments") + paid.data["id"] + "/allocate/", {
            "operation_id": str(uuid.uuid4()), "reason": "Règlement confirmé", "allocations": [{"debt_id": invoice["debt"]["id"], "amount_fcfa": 7000}],
        }, format="json")
        self.assertEqual(allocation.status_code, 201)
        other = self.invoice()
        stock_before = list(StockMovement.objects.values_list("id", "quantity_milli"))
        payload = self.credit_payload(departure)
        result = self.client.post(self.url("credits"), payload, format="json")
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.data["reference_fcfa"], 3000)
        self.assertEqual(result.data["amount_fcfa"], 5000)
        self.assertEqual(result.data["applied_to_origin_fcfa"], 3000)
        self.assertEqual(result.data["remaining_fcfa"], 2000)
        retry = self.client.post(self.url("credits"), payload, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, result.data)
        self.assertEqual(SupplierDebt.objects.get(id=invoice["debt"]["id"]).remaining_fcfa, 0)
        self.assertEqual(SupplierDebt.objects.get(id=other["debt"]["id"]).remaining_fcfa, 10000)
        self.assertEqual(list(StockMovement.objects.values_list("id", "quantity_milli")), stock_before)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, -7000)

    def test_refund_consumes_only_available_credit_and_adds_money_once(self):
        invoice, departure = self.departure()
        credit = self.client.post(self.url("credits"), self.credit_payload(departure, 12000), format="json")
        self.assertEqual(credit.status_code, 201)
        self.assertEqual(credit.data["remaining_fcfa"], 2000)
        url = self.url("credits") + credit.data["id"] + "/refund/"
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-05T00:00:00Z",
                "reason": "Argent effectivement reçu", "method": "orange", "amount_fcfa": 1500}
        result = self.client.post(url, data, format="json")
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.data["after_remaining_fcfa"], 500)
        self.assertEqual(self.client.post(url, data, format="json").data, result.data)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 1500)
        self.assertEqual(self.client.get(self.url("credits") + credit.data["id"] + "/").data["remaining_fcfa"], 500)
        excessive = {**data, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 501}
        self.assertEqual(self.client.post(url, excessive, format="json").status_code, 400)
        self.assertEqual(CashMovement.objects.count(), 1)
        cash = self.client.get(f"/api/v1/shops/{self.shop.id}/cash/")
        self.assertEqual(cash.data["totals_by_method_fcfa"], {"orange": 1500})
        self.assertEqual(cash.data["results"][0]["kind"], "supplier_refund")

    def test_credit_cannot_be_accepted_by_seller_or_against_a_foreign_return(self):
        invoice, departure = self.departure()
        self.authenticate(self.seller)
        self.assertEqual(self.client.post(self.url("credits"), self.credit_payload(departure), format="json").status_code, 403)
        self.assertEqual(self.client.get(self.url("credits")).status_code, 403)
        self.assertFalse(CashMovement.objects.exists())

    def test_three_fractional_returns_keep_exact_original_value_even_if_credits_are_accepted_later(self):
        from catalogue.models import Product
        Product.objects.filter(id=self.product.id).update(unit_kind="weight")
        data = self.invoice_payload()
        data["lines"][0].update(pack_id=None, quantity_milli=1500, unit_price_fcfa=1)
        invoice = self.client.post(self.url(), data, format="json")
        self.assertEqual(invoice.status_code, 201)
        self.assertEqual(invoice.data["total_fcfa"], 2)
        root = self.url() + invoice.data["id"] + "/"
        received = self.client.post(root + "receipts/", self.receipt_payload(invoice.data, 1500), format="json")
        self.assertEqual(received.status_code, 201)
        departures = []
        for _ in range(3):
            departed = self.client.post(root + "returns/", self.return_payload(invoice.data, 500), format="json")
            self.assertEqual(departed.status_code, 201)
            departures.append(departed.data)
        references = []
        for departure in departures:
            credit = self.client.post(self.url("credits"), self.credit_payload(departure, 1), format="json")
            self.assertEqual(credit.status_code, 201)
            references.append(credit.data["reference_fcfa"])
        self.assertEqual(references, [1, 0, 1])
        self.assertEqual(sum(references), 2)

    def test_return_of_another_supplier_is_hidden_and_cannot_reduce_any_debt(self):
        invoice, departure = self.departure()
        supplier = self.client.post(f"/api/v1/shops/{self.shop.id}/suppliers/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Autre fournisseur",
        }, format="json")
        self.assertEqual(supplier.status_code, 201)
        url = f"/api/v1/shops/{self.shop.id}/suppliers/{supplier.data['id']}/credits/"
        self.assertEqual(self.client.post(url, self.credit_payload(departure), format="json").status_code, 404)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 10000)
        self.assertFalse(CashMovement.objects.exists())

    def test_changed_credit_and_refund_retries_preserve_financial_facts(self):
        invoice, departure = self.departure()
        data = self.credit_payload(departure, 12000)
        credit = self.client.post(self.url("credits"), data, format="json")
        self.assertEqual(credit.status_code, 201)
        self.assertEqual(self.client.post(self.url("credits"), {**data, "amount_fcfa": 13000}, format="json").status_code, 409)
        refund = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-05T00:00:00Z",
                  "reason": "Remboursement réel", "method": "cash", "amount_fcfa": 1000}
        url = self.url("credits") + credit.data["id"] + "/refund/"
        self.assertEqual(self.client.post(url, refund, format="json").status_code, 201)
        self.assertEqual(self.client.post(url, {**refund, "amount_fcfa": 900}, format="json").status_code, 409)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 1000)
        self.assertEqual(self.client.get(self.url("credits") + credit.data["id"] + "/").data["remaining_fcfa"], 1000)

    def test_database_rejects_fractional_credit_and_refund_amounts(self):
        from django.db import IntegrityError, transaction, connection
        from purchasing.models import SupplierCredit, SupplierRefund
        invoice, departure = self.departure()
        credit = self.client.post(self.url("credits"), self.credit_payload(departure, 12000), format="json")
        self.assertEqual(credit.status_code, 201)
        refunded = self.client.post(self.url("credits") + credit.data["id"] + "/refund/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-05T00:00:00Z",
            "reason": "Argent reçu", "method": "cash", "amount_fcfa": 1000,
        }, format="json")
        self.assertEqual(refunded.status_code, 201)
        for model, changes in ((SupplierCredit, {"amount_fcfa": 12000.5}), (SupplierCredit, {"reference_fcfa": 3000.5}),
                               (SupplierCredit, {"applied_to_origin_fcfa": 9999.5}), (SupplierCredit, {"remaining_fcfa": 1000.5}),
                               (SupplierRefund, {"amount_fcfa": 1000.5}), (SupplierRefund, {"after_remaining_fcfa": 999.5})):
            with self.subTest(model=model.__name__, changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                field, value = next(iter(changes.items()))
                with connection.cursor() as cursor:
                    cursor.execute(f"UPDATE {model._meta.db_table} SET {field} = %s", [value])

    def test_excess_credit_can_be_explicitly_allocated_to_another_invoice_without_cash(self):
        invoice, departure = self.departure()
        payload = self.credit_payload(departure, 12000)
        credit = self.client.post(self.url("credits"), payload, format="json")
        self.assertEqual(credit.status_code, 201)
        other = self.invoice()
        data = {"operation_id": str(uuid.uuid4()), "reason": "Avoir appliqué à la facture suivante",
                "allocations": [{"debt_id": other["debt"]["id"], "amount_fcfa": 1500}]}
        url = self.url("credits") + credit.data["id"] + "/allocate/"
        result = self.client.post(url, data, format="json")
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.data["after_remaining_fcfa"], 500)
        retry = self.client.post(url, data, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, result.data)
        self.assertEqual(SupplierDebt.objects.get(id=other["debt"]["id"]).remaining_fcfa, 8500)
        self.assertFalse(CashMovement.objects.exists())
        self.assertEqual(self.client.post(self.url("credits"), payload, format="json").data, credit.data)

    def test_credit_history_keeps_acceptance_and_real_refund_with_actor_and_reason(self):
        invoice, departure = self.departure()
        credit = self.client.post(self.url("credits"), self.credit_payload(departure, 12000), format="json")
        self.assertEqual(credit.status_code, 201)
        url = self.url("credits") + credit.data["id"] + "/"
        refund = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-05T00:00:00Z",
                  "reason": "Remboursement réellement reçu", "method": "cash", "amount_fcfa": 1000}
        self.assertEqual(self.client.post(url + "refund/", refund, format="json").status_code, 201)
        history = self.client.get(url + "history/")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.data["count"], 2)
        self.assertEqual([row["action"] for row in history.data["results"]], ["supplier_credit_accepted", "supplier_refund_received"])
        self.assertEqual(history.data["results"][1]["reason"], refund["reason"])
        self.assertEqual(history.data["results"][1]["actor_id"], str(self.owner.id))
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(url + "history/").status_code, 403)
