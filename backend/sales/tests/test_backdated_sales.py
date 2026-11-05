"""US-35: a manager may record an actual forgotten sale without rewriting receipt time."""
from datetime import datetime, timedelta, timezone as utc
from django.test import TestCase
from django.utils import timezone
from sales.tests import test_sales as sale_tests


class BackdatedSaleTests(TestCase):
    setUp = sale_tests.CashSaleTests.setUp
    authenticate = sale_tests.CashSaleTests.authenticate
    payload = sale_tests.CashSaleTests.payload
    url = sale_tests.CashSaleTests.url

    def test_manager_date_and_reason_reach_sale_stock_cash_and_durable_receipt_once(self):
        from sales.models import Sale
        from stock.models import StockMovement
        from cash.models import CashMovement
        from common.models import Activity
        self.authenticate(self.owner)
        data = self.payload(occurred_at="2026-01-01T10:00:00+01:00", reason="Vente oubliée, justificatif vérifié")
        created = self.client.post(self.url(), data, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        occurred = datetime(2026, 1, 1, 9, tzinfo=utc.utc)
        sale = Sale.objects.get()
        self.assertEqual(sale.occurred_at, occurred)
        self.assertGreater(sale.created_at, occurred)
        self.assertEqual(StockMovement.objects.get(kind="sale").occurred_at, occurred)
        self.assertEqual(set(CashMovement.objects.values_list("occurred_at", flat=True)), {occurred})
        self.assertEqual(Activity.objects.get(id=data["operation_id"]).reason, "Vente oubliée, justificatif vérifié")
        self.assertEqual(self.client.post(self.url(), data, format="json").data, created.data)
        self.assertEqual(StockMovement.objects.filter(kind="sale").count(), 1)
        self.assertEqual(CashMovement.objects.count(), 2)

    def test_seller_cannot_choose_an_effective_date_even_with_a_reason(self):
        from sales.models import Sale
        from cash.models import CashMovement
        response = self.client.post(self.url(), self.payload(occurred_at="2026-01-01T10:00:00Z", reason="Déclaration vendeur"), format="json")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(CashMovement.objects.exists())

    def test_manager_date_requires_reason_and_rejects_future_or_reason_without_date(self):
        from sales.models import Sale
        self.authenticate(self.owner)
        for changes in ({"occurred_at": "2026-01-01T10:00:00Z"},
                        {"occurred_at": "2026-01-01T10:00:00Z", "reason": " "},
                        {"occurred_at": (timezone.now() + timedelta(days=1)).isoformat(), "reason": "Future"},
                        {"reason": "Date manquante"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.client.post(self.url(), self.payload(**changes), format="json").status_code, 400)
        self.assertFalse(Sale.objects.exists())

    def test_backdated_credit_reaches_debt_and_customer_ledger_and_replay_cannot_change_date(self):
        from customers.models import Customer, CustomerDebt, CustomerLedgerEntry
        from sales.models import Sale
        import uuid
        self.authenticate(self.owner)
        customer = Customer.objects.create(shop=self.shop, name="Client")
        data = self.payload(mode="credit", customer_id=str(customer.id), occurred_at="2026-01-01T09:00:00Z",
            reason="Vente à crédit oubliée", payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 4000}])
        response = self.client.post(self.url(), data, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        occurred = datetime(2026, 1, 1, 9, tzinfo=utc.utc)
        self.assertEqual(CustomerDebt.objects.get().occurred_at, occurred)
        self.assertEqual(CustomerDebt.objects.get().remaining_fcfa, 6000)
        self.assertEqual(set(CustomerLedgerEntry.objects.values_list("occurred_at", flat=True)), {occurred})
        self.assertTrue(all(row.recorded_at > occurred for row in CustomerLedgerEntry.objects.all()))
        changed = {**data, "occurred_at": "2026-01-02T09:00:00Z"}
        self.assertEqual(self.client.post(self.url(), changed, format="json").status_code, 409)
        self.assertEqual(Sale.objects.count(), 1)

    def test_manager_permission_is_rechecked_before_returning_old_backdated_receipt(self):
        from identity.models import Membership
        self.member.role = Membership.Role.MANAGER
        self.member.save(update_fields=["role"])
        data = self.payload(occurred_at="2026-01-01T09:00:00Z", reason="Vente oubliée")
        first = self.client.post(self.url(), data, format="json")
        self.assertEqual(first.status_code, 201)
        self.member.role = Membership.Role.SELLER
        self.member.save(update_fields=["role"])
        self.assertEqual(self.client.post(self.url(), data, format="json").status_code, 403)

    def test_manager_can_read_dated_sale_journal_but_seller_cannot(self):
        self.authenticate(self.owner)
        data = self.payload(occurred_at="2026-01-01T09:00:00Z", reason="Justificatif de vente retrouvé")
        sale = self.client.post(self.url(), data, format="json")
        self.assertEqual(sale.status_code, 201)
        history_url = self.url(sale.data["id"]) + "history/"
        history = self.client.get(history_url)
        self.assertEqual(history.status_code, 200, history.content)
        self.assertEqual(history.data["count"], 1)
        entry = history.data["results"][0]
        self.assertEqual(entry["reason"], "Justificatif de vente retrouvé")
        self.assertEqual(entry["details"]["request"]["occurred_at"], "2026-01-01T09:00:00Z")
        self.assertEqual(entry["actor_id"], str(self.owner.id))
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(history_url).status_code, 403)

    def test_late_sale_revises_count_expectations_without_rewriting_physical_observations(self):
        from stock.models import StockMovement, StockCountAdjustment
        from cash.models import CashMovement
        import uuid
        self.authenticate(self.owner)
        StockMovement.objects.filter(kind="opening").update(occurred_at=datetime(2025, 12, 31, tzinfo=utc.utc))
        stock_counts = f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/stock-counts/"
        stock = self.client.post(stock_counts, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "physical_milli": 10000, "counted_at": "2026-01-02T10:00:00Z", "reason": "Stock compté"}, format="json")
        self.assertEqual(stock.status_code, 201)
        cash_counts = f"/api/v1/shops/{self.shop.id}/cash-counts/"
        cash = self.client.post(cash_counts, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "physical_fcfa": 0, "method": "cash", "counted_at": "2026-01-02T10:00:00Z", "reason": "Caisse comptée"}, format="json")
        self.assertEqual(cash.status_code, 201)
        sale = self.client.post(self.url(), self.payload(occurred_at="2026-01-01T09:00:00Z", reason="Vente oubliée"), format="json")
        self.assertEqual(sale.status_code, 201)
        revised_stock = self.client.get(stock_counts + stock.data["id"] + "/").data
        self.assertEqual(revised_stock["physical_milli"], 10000)
        self.assertEqual(revised_stock["expected_milli"], 8000)
        self.assertTrue(revised_stock["expected_revised"])
        revised_cash = self.client.get(cash_counts + cash.data["id"] + "/").data
        self.assertEqual(revised_cash["physical_fcfa"], 0)
        self.assertEqual(revised_cash["expected_fcfa"], 4000)
        self.assertTrue(revised_cash["expected_revised"])
        self.assertFalse(StockCountAdjustment.objects.exists())
        self.assertEqual(CashMovement.objects.count(), 2)

    def test_manual_date_validation_and_permission_are_localized_in_english(self):
        self.authenticate(self.owner)
        invalid = self.client.post(self.url(), self.payload(occurred_at="2026-01-01T09:00:00Z"), format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["reason"][0], "A reason is required for a manual effective date.")
        self.authenticate(self.seller)
        denied = self.client.post(self.url(), self.payload(occurred_at="2026-01-01T09:00:00Z", reason="Forgotten sale"), format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["detail"], "Only a manager may set a sale date manually.")
