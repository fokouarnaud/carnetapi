"""Q177: supplier debt, advances and credits stay separate in chronological statements."""
import uuid
from django.db.models import Sum
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from purchasing.tests import test_purchases as purchase_tests


class SupplierStatementTests(TestCase):
    setUp = purchase_tests.PurchaseTests.setUp
    authenticate = purchase_tests.PurchaseTests.authenticate

    def test_ledger_prevents_duplicate_effects_and_inconsistent_balances(self):
        from common.models import Activity
        from purchasing.models import SupplierLedgerEntry
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="opening", target_id=uuid.uuid4(), reason="Solde")
        data = dict(shop=self.shop, supplier_id=self.supplier_id, operation=event, source_id=uuid.uuid4(), kind="opening_debt",
                    occurred_at=timezone.now(), recorded_at=event.created_at, delta_fcfa=7000, debt_delta_fcfa=7000)
        row = SupplierLedgerEntry.objects.create(**data)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SupplierLedgerEntry.objects.create(**{**data, "source_id": uuid.uuid4()})
        for changes in ({"delta_fcfa": 7001}, {"state": "unknown"}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                SupplierLedgerEntry.objects.filter(id=row.id).update(**changes)

    def url(self, suffix="statement/"):
        return f"/api/v1/shops/{self.shop.id}/suppliers/{self.supplier_id}/" + suffix

    def post(self, suffix, data):
        response = self.client.post(self.url(suffix), data, format="json")
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.data

    def populate_openings(self):
        debt = self.post("debts/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 10000,
            "occurred_at": "2026-01-01T10:00:00Z", "reason": "Dette antérieure"})
        opening = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "balance_kind": "advance", "amount_fcfa": 3000,
            "occurred_at": "2026-01-02T10:00:00Z", "reason": "Avance antérieure"}
        advance = self.post("opening-balances/", opening)
        credit = self.post("opening-balances/", {**opening, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "balance_kind": "credit", "amount_fcfa": 2000, "occurred_at": "2026-01-03T10:00:00Z"})
        self.post(f"credits/{advance['id']}/allocate/", {"operation_id": str(uuid.uuid4()), "reason": "Affectation explicite",
            "allocations": [{"debt_id": debt["id"], "amount_fcfa": 2000}]})
        self.post(f"credits/{credit['id']}/refund/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "occurred_at": timezone.now().isoformat(), "reason": "Argent reçu", "method": "orange", "amount_fcfa": 500})

    def test_statement_separates_three_balances_and_only_real_refund_changes_cash(self):
        from cash.models import CashMovement
        self.populate_openings()
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["count"], 5)
        self.assertEqual([row["kind"] for row in response.data["results"]], ["opening_debt", "opening_advance", "opening_credit", "advance_allocation", "supplier_refund"])
        self.assertEqual([row["delta_fcfa"] for row in response.data["results"]], [10000, -3000, -2000, 0, 500])
        self.assertEqual([row["balance_after_fcfa"] for row in response.data["results"]], [10000, 7000, 5000, 5000, 5500])
        self.assertEqual(response.data["closing_debt_fcfa"], 8000)
        self.assertEqual(response.data["closing_advance_fcfa"], 1000)
        self.assertEqual(response.data["closing_credit_fcfa"], 1500)
        self.assertEqual(response.data["closing_balance_fcfa"], 5500)
        self.assertEqual(response.data["results"][3]["allocations"][0]["amount_fcfa"], 2000)
        self.assertEqual(CashMovement.objects.aggregate(total=Sum("amount_fcfa"))["total"], 500)
        self.assertTrue(response.data["provisional"])

    def test_invoice_payment_components_and_allocation_count_money_only_once(self):
        from cash.models import CashMovement
        data = purchase_tests.PurchaseTests.payload(self)
        self.post("invoices/", data)
        debt = self.client.get(self.url("debts/")).data["results"][0]
        payment = self.post("payments/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "occurred_at": "2026-01-02T10:00:00Z", "reason": "Paiement réel", "parts": [
                {"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 2000},
                {"id": str(uuid.uuid4()), "method": "orange", "amount_fcfa": 3000}]})
        self.post(f"payments/{payment['id']}/allocate/", {"operation_id": str(uuid.uuid4()), "reason": "Paiement affecté",
            "allocations": [{"debt_id": debt["id"], "amount_fcfa": 5000}]})
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual([row["delta_fcfa"] for row in response.data["results"]], [10000, -5000, 0])
        self.assertEqual(response.data["closing_debt_fcfa"], 5000)
        self.assertEqual(response.data["closing_advance_fcfa"], 0)
        self.assertEqual(len(response.data["results"][1]["parts"]), 2)
        self.assertEqual(CashMovement.objects.aggregate(total=Sum("amount_fcfa"))["total"], -5000)

    def test_ledger_equation_is_exact_when_sqlite_intermediate_would_overflow(self):
        from common.models import Activity
        from purchasing.models import SupplierLedgerEntry
        maximum = 9223372036854775807
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="reclassification", target_id=uuid.uuid4(), reason="Correction explicite")
        row = SupplierLedgerEntry.objects.create(shop=self.shop, supplier_id=self.supplier_id, operation=event,
            source_id=uuid.uuid4(), kind="reclassification", occurred_at=timezone.now(), recorded_at=event.created_at,
            delta_fcfa=-maximum, debt_delta_fcfa=-maximum, advance_delta_fcfa=maximum, credit_delta_fcfa=-maximum)
        row.refresh_from_db()
        self.assertEqual(row.delta_fcfa, -9223372036854775807)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SupplierLedgerEntry.objects.filter(id=row.id).update(delta_fcfa=-9223372036854775806)

    def test_sql_pagination_keeps_full_totals_and_original_cutoff_after_a_late_arrival(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.populate_openings()
        with CaptureQueriesContext(connection) as queries:
            first = self.client.get(self.url(), {"page_size": 2})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["count"], 5)
        self.assertEqual(len(first.data["results"]), 2)
        self.assertEqual(first.data["closing_balance_fcfa"], 5500)
        self.assertIn("as_of=", first.data["next"])
        self.assertTrue(any('supplierledgerentry' in query['sql'] and 'LIMIT 2' in query['sql'] for query in queries))
        self.post("debts/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 1000,
            "occurred_at": "2026-01-01T11:00:00Z", "reason": "Dette oubliée"})
        second = self.client.get(first.data["next"])
        self.assertEqual(second.data["as_of"], first.data["as_of"])
        self.assertEqual(second.data["count"], 5)
        self.assertEqual(second.data["closing_balance_fcfa"], 5500)
        self.assertFalse({row["id"] for row in first.data["results"]} & {row["id"] for row in second.data["results"]})
        self.assertEqual(self.client.get(self.url()).data["closing_balance_fcfa"], 6500)

    def test_local_inclusive_period_has_prior_balance_and_excludes_next_local_day(self):
        for occurred_at, amount in (("2026-01-01T22:59:59Z", 10), ("2026-01-01T23:00:00Z", 20),
                                    ("2026-01-02T22:59:59Z", 30), ("2026-01-02T23:00:00Z", 40)):
            self.post("debts/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": amount,
                "occurred_at": occurred_at, "reason": "Date effective"})
        response = self.client.get(self.url(), {"date_from": "2026-01-02", "date_to": "2026-01-02"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["opening_balance_fcfa"], 10)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(response.data["closing_balance_fcfa"], 60)
        self.assertEqual([row["balance_after_fcfa"] for row in response.data["results"]], [30, 60])

    def test_pending_and_rejected_rows_never_inflate_validated_balances(self):
        from common.models import Activity
        from purchasing.models import SupplierLedgerEntry
        for state, amount in (("validated", 1000), ("pending", 2000), ("rejected", 3000)):
            event = Activity.objects.create(shop=self.shop, actor=self.owner, action="source", target_id=uuid.uuid4(), reason="État explicite")
            SupplierLedgerEntry.objects.create(shop=self.shop, supplier_id=self.supplier_id, operation=event, source_id=event.id,
                kind="opening_debt", state=state, occurred_at=event.created_at, recorded_at=event.created_at,
                delta_fcfa=amount, debt_delta_fcfa=amount)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(response.data["closing_balance_fcfa"], 1000)
        self.assertEqual(response.data["closing_debt_fcfa"], 1000)
        self.assertEqual(response.data["pending_count"], 1)
        self.assertEqual(response.data["pending_delta_fcfa"], 2000)
        self.assertEqual([row["balance_after_fcfa"] for row in response.data["results"]], [1000, 1000, 1000])

    def test_totals_exceeding_sqlite_int64_are_exact_and_current_access_is_required(self):
        from common.models import Activity
        from purchasing.models import SupplierLedgerEntry, Supplier
        from identity.models import DeviceSession
        for number in range(2):
            event = Activity.objects.create(shop=self.shop, actor=self.owner, action="source", target_id=uuid.uuid4(), reason="Montant exact")
            SupplierLedgerEntry.objects.create(shop=self.shop, supplier_id=self.supplier_id, operation=event, source_id=event.id,
                kind="opening_debt", occurred_at=event.created_at, recorded_at=event.created_at,
                delta_fcfa=9223372036854775807, debt_delta_fcfa=9223372036854775807)
        response = self.client.get(self.url(), {"page_size": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["closing_balance_fcfa"], 18446744073709551614)
        other = Supplier.objects.create(shop=self.shop, name="Autre")
        other_url = self.url().replace(str(self.supplier_id), str(other.id))
        self.assertEqual(self.client.get(other_url).data["count"], 0)
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.authenticate(self.owner)
        DeviceSession.objects.filter(installation__user=self.owner).update(revoked_at=timezone.now())
        self.assertEqual(self.client.get(self.url()).status_code, 401)

    def test_physical_return_credit_and_refund_remain_distinct_statement_events(self):
        invoice = self.post("invoices/", purchase_tests.PurchaseTests.payload(self))
        self.post(f"invoices/{invoice['id']}/receipts/", purchase_tests.PurchaseTests.receipt_payload(self, invoice, 10000))
        departure = self.post(f"invoices/{invoice['id']}/returns/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "occurred_at": "2026-01-03T00:00:00Z", "reason": "Marchandises parties", "lines": [
                {"line_id": invoice["lines"][0]["id"], "quantity_milli": 5000}]})
        credit_data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "return_id": departure["id"],
            "amount_fcfa": 12000, "occurred_at": "2026-01-04T00:00:00Z", "reason": "Avoir accepté explicitement"}
        credit = self.post("credits/", credit_data)
        self.post("credits/", credit_data)
        self.post(f"credits/{credit['id']}/refund/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "amount_fcfa": 500, "method": "cash", "occurred_at": "2026-01-05T00:00:00Z", "reason": "Argent reçu"})
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 4)
        self.assertEqual([row["kind"] for row in response.data["results"]], ["invoice", "supplier_return", "return_credit", "supplier_refund"])
        self.assertEqual([row["delta_fcfa"] for row in response.data["results"]], [10000, 0, -12000, 500])
        self.assertEqual(response.data["closing_debt_fcfa"], 0)
        self.assertEqual(response.data["closing_credit_fcfa"], 1500)
        self.assertEqual(response.data["closing_balance_fcfa"], -1500)
        self.assertEqual(response.data["results"][0]["external_reference"], "FOURN-2026-001")
