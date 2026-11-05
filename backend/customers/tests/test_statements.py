"""US-24/41: exact chronological balances and SQL pagination after authorization."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from customers.models import Customer, CustomerLedgerEntry
from identity.models import DeviceSession, Installation, Membership, Shop, User
from catalogue.models import Product


class CustomerStatementTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="statement-owner")
        self.seller = User.objects.create_user(username="statement-seller")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        self.other = Shop.objects.create(name="B", owner=self.owner)
        self.customer = Customer.objects.create(shop=self.shop, name="Client")
        self.foreign = Customer.objects.create(shop=self.other, name="Autre client")
        Membership.objects.create(shop=self.shop, user=self.seller)
        self.client = APIClient()
        self.authenticate(self.owner)

    def authenticate(self, user):
        device = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def base(self, customer=None):
        return f"/api/v1/shops/{self.shop.id}/clients/{(customer or self.customer).id}/"

    def post(self, suffix, data):
        response = self.client.post(self.base() + suffix, data, format="json")
        self.assertIn(response.status_code, (200, 201))
        return response.data

    def populate(self):
        debt = self.post("debts/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 10000,
                                    "occurred_at": "2026-01-01T00:00:00Z", "reason": "Solde antérieur"})
        self.post("payments/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                                "parts": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 5000}],
                                "allocations": [{"id": str(uuid.uuid4()), "debt_id": debt["id"], "amount_fcfa": 5000}]})
        source = self.post("payments/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                                          "parts": [{"id": str(uuid.uuid4()), "method": "orange", "amount_fcfa": 3000}], "allocations": []})
        self.post(f"payments/{source['id']}/allocate/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Affectation",
                                                       "allocations": [{"id": str(uuid.uuid4()), "debt_id": debt["id"], "amount_fcfa": 2000}]})
        self.post(f"payments/{source['id']}/refund/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Rendu effectif",
                                                     "method": "orange", "amount_fcfa": 1000})

    def test_statement_preserves_allocations_cash_and_balance_after_each_event(self):
        self.populate()
        response = self.client.get(self.base() + "statement/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 5)
        self.assertEqual([row["kind"] for row in response.data["results"]], ["opening_debt", "collection", "collection", "allocation", "refund"])
        self.assertEqual([row["delta_fcfa"] for row in response.data["results"]], [10000, -5000, -3000, 0, 1000])
        self.assertEqual([row["balance_after_fcfa"] for row in response.data["results"]], [10000, 5000, 2000, 2000, 3000])
        self.assertEqual(response.data["opening_balance_fcfa"], 0)
        self.assertEqual(response.data["closing_balance_fcfa"], 3000)
        self.assertEqual(response.data["closing_debt_fcfa"], 3000)
        self.assertEqual(response.data["closing_unallocated_fcfa"], 0)
        self.assertEqual(response.data["results"][1]["allocations"][0]["applied_amount_fcfa"], 5000)
        self.assertEqual(response.data["results"][3]["allocations"][0]["applied_amount_fcfa"], 2000)
        self.assertEqual(response.data["results"][4]["actor_id"], str(self.owner.id))
        self.assertEqual(response.data["pending_count"], 0)
        self.assertTrue(response.data["provisional"])

    def test_pagination_uses_full_filter_totals_and_keeps_snapshot_on_next_link(self):
        self.populate()
        first = self.client.get(self.base() + "statement/", {"page_size": 2})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["count"], 5)
        self.assertEqual(first.data["closing_balance_fcfa"], 3000)
        self.assertIn("as_of=", first.data["next"])
        second = self.client.get(first.data["next"])
        self.assertEqual(second.data["as_of"], first.data["as_of"])
        self.assertEqual([row["balance_after_fcfa"] for row in second.data["results"]], [2000, 2000])
        self.assertFalse({row["id"] for row in first.data["results"]} & {row["id"] for row in second.data["results"]})

    def test_local_period_includes_prior_balance_and_all_filtered_movements(self):
        self.populate()
        response = self.client.get(self.base() + "statement/", {"date_from": "2026-10-01", "date_to": "2026-10-31"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["opening_balance_fcfa"], 10000)
        self.assertEqual(response.data["count"], 4)
        self.assertEqual(response.data["closing_balance_fcfa"], 3000)

    def test_sale_and_immediate_payment_are_separate_and_not_double_counted_as_debt(self):
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=10000)
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "mode": "credit", "customer_id": str(self.customer.id),
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 2000}]}
        self.assertEqual(self.client.post(f"/api/v1/shops/{self.shop.id}/sales/", data, format="json").status_code, 201)
        response = self.client.get(self.base() + "statement/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["delta_fcfa"] for row in response.data["results"]], [10000, -2000])
        self.assertEqual(response.data["closing_balance_fcfa"], 8000)
        self.assertEqual(response.data["closing_debt_fcfa"], 8000)

    def test_statement_requires_manager_and_hides_other_shop_customer(self):
        self.assertEqual(self.client.get(self.base(self.foreign) + "statement/").status_code, 404)
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.base() + "statement/").status_code, 403)

    def test_bad_period_future_snapshot_and_unknown_query_fields_are_rejected(self):
        for params in [{"date_from": "bad"}, {"date_from": "2026-11-01", "date_to": "2026-10-01"},
                       {"as_of": (timezone.now() + timedelta(days=1)).isoformat()}, {"customer_id": str(self.foreign.id)}]:
            with self.subTest(params=params):
                self.assertEqual(self.client.get(self.base() + "statement/", params).status_code, 400)

    def test_next_page_excludes_late_backdated_entry_from_original_snapshot(self):
        self.populate()
        first = self.client.get(self.base() + "statement/", {"page_size": 2})
        self.assertEqual(first.status_code, 200)
        self.post("debts/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 1000,
                             "occurred_at": "2026-01-02T00:00:00Z", "reason": "Solde complémentaire documenté"})
        next_page = self.client.get(first.data["next"])
        self.assertEqual(next_page.data["count"], 5)
        self.assertEqual(next_page.data["closing_balance_fcfa"], 3000)
        self.assertEqual(next_page.data["as_of"], first.data["as_of"])
        current = self.client.get(self.base() + "statement/")
        self.assertEqual(current.data["count"], 6)
        self.assertEqual(current.data["closing_balance_fcfa"], 4000)

    def test_wide_integer_totals_are_not_truncated_by_sqlite_sum(self):
        for _ in range(2):
            self.post("debts/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 9223372036854775807,
                                 "occurred_at": "2026-01-01T00:00:00Z", "reason": "Cas limite exact"})
        response = self.client.get(self.base() + "statement/", {"page_size": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["closing_balance_fcfa"], 18446744073709551614)
        self.assertEqual(response.data["closing_debt_fcfa"], 18446744073709551614)

    def test_pending_and_rejected_rows_do_not_change_validated_balance(self):
        self.post("debts/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 10000,
                             "occurred_at": "2026-01-01T00:00:00Z", "reason": "Solde validé"})
        for state, delta in [("pending", -2000), ("rejected", -3000)]:
            event = Activity.objects.create(shop=self.shop, actor=self.owner, action="statement_fixture", target_id=self.customer.id, reason="État à qualifier")
            CustomerLedgerEntry.objects.create(shop=self.shop, customer=self.customer, operation=event, source_id=uuid.uuid4(),
                                               kind="collection", delta_fcfa=delta, state=state, occurred_at=timezone.now(), recorded_at=event.created_at)
        response = self.client.get(self.base() + "statement/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(response.data["closing_balance_fcfa"], 10000)
        self.assertEqual(response.data["pending_count"], 1)
        self.assertEqual(response.data["pending_delta_fcfa"], -2000)
        self.assertEqual([row["balance_after_fcfa"] for row in response.data["results"]], [10000, 10000, 10000])

    def test_page_is_limited_in_sql_and_unauthorized_request_does_not_read_ledger(self):
        self.populate()
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(self.base() + "statement/", {"page_size": 2})
        self.assertEqual(response.status_code, 200)
        page_queries = [item["sql"] for item in captured.captured_queries
                        if 'FROM "core_customerledgerentry"' in item["sql"] and "LIMIT 2" in item["sql"]]
        self.assertEqual(len(page_queries), 1)
        self.authenticate(self.seller)
        with CaptureQueriesContext(connection) as unauthorized:
            denied = self.client.get(self.base() + "statement/")
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(any('FROM "core_customerledgerentry"' in item["sql"] for item in unauthorized.captured_queries))
