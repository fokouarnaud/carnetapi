from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
import uuid

from django.db import connections
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from common.models import Activity
from cash.models import CashMovement
from customers.models import Customer, CustomerCredit, CustomerDebt, CustomerPayment, PaymentRegularization
from identity.models import DeviceSession, Installation, Membership, Shop, User
from catalogue.models import Product, ProductPack
from sales.models import Sale, SalePayment
from stock.models import StockMovement


class SQLiteConcurrencyChecks(TransactionTestCase):
    """Run explicitly with config.concurrency_test_settings (not shared-memory SQLite)."""

    def test_matching_client_creations_keep_both_uuids_and_only_retry_the_original_operation(self):
        bearer = self.owner_bearer()
        path = f"/api/v1/shops/{self.shop.id}/clients/"
        first = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Client commun", "phone": "+237600000001"}
        second = {**first, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4())}
        results = self.simultaneous(path, first, bearer, second_body=second)
        self.assertEqual(sorted(status for status, _ in results), [201, 201])
        self.assertEqual(sorted(body["has_possible_duplicate"] for _, body in results), [False, True])
        receipts = {body["id"]: body for _, body in results}
        self.assertEqual(set(receipts), {first["id"], second["id"]})
        self.assertEqual(Customer.objects.count(), 2)
        self.assertEqual(Activity.objects.filter(action="client_created").count(), 2)
        retries = self.simultaneous(path, first, bearer)
        self.assertEqual([status for status, _ in retries], [200, 200])
        self.assertEqual([body for _, body in retries], [receipts[first["id"]], receipts[first["id"]]])
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
        current = client.get(path)
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.data["count"], 2)
        self.assertTrue(all(row["has_possible_duplicate"] for row in current.data["results"]))
        matches = client.get(path + first["id"] + "/possible-duplicates/")
        self.assertEqual(matches.status_code, 200)
        self.assertEqual(matches.data["count"], 1)
        self.assertEqual(matches.data["results"][0]["id"], second["id"])
        self.assertEqual(Customer.objects.count(), 2)
        self.assertFalse(CustomerDebt.objects.exists())
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

    def test_shop_creation_retries_and_uuid_conflicts_never_duplicate_ownership(self):
        bearer = self.owner_bearer()
        for mode in ("retry", "entity_conflict", "operation_conflict"):
            first = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Nouvelle boutique"}
            second = dict(first)
            if mode == "entity_conflict":
                second["operation_id"] = str(uuid.uuid4())
            elif mode == "operation_conflict":
                second["id"] = str(uuid.uuid4())
            results = self.simultaneous("/api/v1/shops/", first, bearer, second_body=second)
            self.assertEqual(sorted(status for status, _ in results), [200, 201] if mode == "retry" else [201, 409])
            if mode == "retry":
                self.assertEqual(results[0][1], results[1][1])
            winner = next(body for status, body in results if status == 201)
            self.assertEqual(Shop.objects.get(id=winner["id"]).owner_id, self.owner.id)
        self.assertEqual(Shop.objects.count(), 4)
        self.assertEqual(Activity.objects.filter(action="shop_created").count(), 3)
        self.assertEqual(Membership.objects.count(), 1)
        self.assertFalse(StockMovement.objects.exists())
        self.assertFalse(CashMovement.objects.exists())

    def test_cost_issue_cutoff_waits_for_inflight_sale_and_keeps_later_pages_stable(self):
        from threading import Event
        from django.db import transaction
        from stock.models import StockCostSnapshot
        bearer = self.owner_bearer()
        product = Product.objects.create(shop=self.shop, name="Coût inconnu", unit_kind="piece", unit_label="pièce", sale_price_fcfa=100)
        sales_path = f"/api/v1/shops/{self.shop.id}/sales/"
        issues_path = f"/api/v1/shops/{self.shop.id}/sale-cost-issues/"

        def client():
            result = APIClient()
            result.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
            return result

        def body():
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 100}]}

        initial = client().post(sales_path, body(), format="json")
        self.assertEqual(initial.status_code, 201)
        writer_ready, reader_begins = Event(), Event()

        def write_sale():
            try:
                with transaction.atomic():
                    response = client().post(sales_path, body(), format="json")
                    self.assertEqual(response.status_code, 201)
                    writer_ready.set()
                    self.assertTrue(reader_begins.wait(10), "Reader never attempted its transaction")
                return response.data
            finally:
                connections.close_all()

        def read_page():
            try:
                self.assertTrue(writer_ready.wait(10), "Sale was not recorded")
                def observe_begin(execute, sql, params, many, context):
                    if sql.strip().upper() == "BEGIN IMMEDIATE":
                        reader_begins.set()
                    return execute(sql, params, many, context)
                with connections["default"].execute_wrapper(observe_begin):
                    response = client().get(issues_path, {"page_size": 1})
                self.assertEqual(response.status_code, 200)
                return response.data
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(write_sale)
            reader = pool.submit(read_page)
            written, first_page = writer.result(timeout=30), reader.result(timeout=30)
        self.assertEqual(first_page["count"], 2)
        self.assertEqual(first_page["totals"], {"unknown_lines": 2, "provisional_lines": 0})
        self.assertEqual(client().post(sales_path, body(), format="json").status_code, 201)
        following = client().get(first_page["next"])
        self.assertEqual(following.status_code, 200)
        self.assertEqual(following.data["count"], 2)
        self.assertEqual(following.data["results"][0]["sale_id"], written["id"])
        self.assertEqual(following.data["as_of"], first_page["as_of"])
        self.assertIsNone(following.data["next"])
        self.assertEqual(client().get(issues_path).data["count"], 3)
        self.assertEqual(StockCostSnapshot.objects.count(), 3)
        self.assertEqual(StockMovement.objects.count(), 3)
        self.assertEqual(sum(CashMovement.objects.values_list("amount_fcfa", flat=True)), 300)

    def test_concurrent_stock_shortage_review_retries_and_distinct_decisions(self):
        from stock.models import StockShortageReview
        bearer = self.owner_bearer()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
        for physical, same_operation in ((1000, True), (0, False)):
            product = Product.objects.create(shop=self.shop, name="Manque contrôlé", unit_kind="piece", unit_label="pièce", sale_price_fcfa=100)
            opening = Activity.objects.create(shop=self.shop, actor=self.owner, action="product_created", target_id=product.id, reason="Ouverture")
            StockMovement.objects.create(product=product, actor=self.owner, operation=opening, kind="opening", quantity_milli=1000,
                occurred_at=timezone.now(), unit_cost_micros=1000000000, reason="Ouverture")
            sale = client.post(f"/api/v1/shops/{self.shop.id}/sales/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 2000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 200}]}, format="json")
            self.assertEqual(sale.status_code, 201)
            base = f"/api/v1/shops/{self.shop.id}/products/{product.id}/"
            count = client.post(base + "stock-counts/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "physical_milli": physical, "counted_at": timezone.now().isoformat(), "reason": "Stock compté"}, format="json")
            self.assertEqual(count.status_code, 201)
            correction = client.post(base + "stock-counts/" + count.data["id"] + "/confirm/", {"operation_id": str(uuid.uuid4()),
                "expected_milli": -1000, "applied_adjustment_milli": 0, "reason": "Correction explicite"}, format="json")
            self.assertEqual(correction.status_code, 201)
            path = base + "stock-shortage-reviews/"
            preview = client.post(path + "preview/", {"count_id": count.data["id"]}, format="json")
            self.assertEqual(preview.status_code, 200)
            first = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "count_id": count.data["id"],
                "preview_token": preview.data["preview_token"], "reason": "Manque examiné"}
            second = first if same_operation else {**first, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4())}
            results = self.simultaneous(path, first, bearer, second_body=second)
            self.assertEqual(sorted(status for status, _ in results), [200, 201] if same_operation else [201, 409])
            if same_operation:
                self.assertEqual(results[0][1], results[1][1])
            winner = first if results[0][0] in (200, 201) else second
            self.assertEqual(client.post(path, winner, format="json").status_code, 200)
            self.assertFalse(client.get(base + "stock-valuations/current/").data["has_unresolved_shortage"])
        self.assertEqual(StockShortageReview.objects.count(), 2)
        self.assertEqual(StockMovement.objects.count(), 6)
        self.assertEqual(sum(CashMovement.objects.values_list("amount_fcfa", flat=True)), 400)

    def test_concurrent_stock_valuation_retries_once_and_distinct_decisions_need_fresh_preview(self):
        from stock.models import StockValuationAdjustment
        product = Product.objects.create(shop=self.shop, name="Valeur confirmée", unit_kind="piece", unit_label="pièce", sale_price_fcfa=100)
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="product_created", target_id=product.id, reason="Stock initial")
        StockMovement.objects.create(product=product, actor=self.owner, operation=event, kind="opening",
            quantity_milli=1000, occurred_at=timezone.now(), reason="Coût inconnu")
        bearer = self.owner_bearer()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
        path = f"/api/v1/shops/{self.shop.id}/products/{product.id}/stock-valuations/"
        preview = client.post(path + "preview/", {"value_fcfa": 600}, format="json")
        self.assertEqual(preview.status_code, 200)
        body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "value_fcfa": 600,
            "reason": "Valeur contrôlée", "preview_token": preview.data["preview_token"]}
        same = self.simultaneous(path, body, bearer)
        self.assertEqual(sorted(status for status, _ in same), [200, 201])
        self.assertEqual(same[0][1], same[1][1])
        self.assertEqual(StockValuationAdjustment.objects.count(), 1)
        fresh = client.post(path + "preview/", {"value_fcfa": 600}, format="json")
        first = {**body, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "preview_token": fresh.data["preview_token"]}
        second = {**first, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4())}
        distinct = self.simultaneous(path, first, bearer, second_body=second)
        self.assertEqual(sorted(status for status, _ in distinct), [201, 409])
        self.assertEqual(StockValuationAdjustment.objects.count(), 2)
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertFalse(CashMovement.objects.exists())
        self.assertEqual(client.get(path + "current/").data["stock_value_nanos_fcfa"], "600000000000")
        # A corrected legacy opening cost invalidates both earlier value confirmations.
        StockMovement.objects.filter(product=product, kind="opening").update(unit_cost_micros=1000000000)
        sale = client.post(f"/api/v1/shops/{self.shop.id}/sales/", {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
            "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 100}]}, format="json")
        self.assertEqual(sale.status_code, 201)
        empty = client.post(path + "preview/", {"value_fcfa": 0}, format="json")
        self.assertEqual(empty.status_code, 200)
        closure = {**body, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "value_fcfa": 0,
            "preview_token": empty.data["preview_token"], "reason": "Écart du stock nul examiné"}
        closed = self.simultaneous(path, closure, bearer)
        self.assertEqual(sorted(status for status, _ in closed), [200, 201])
        self.assertEqual(closed[0][1], closed[1][1])
        self.assertEqual(closed[0][1]["valuation_action"], "close_empty_gap")
        self.assertEqual(StockValuationAdjustment.objects.count(), 3)
        self.assertFalse(client.get(path + "current/").data["has_unresolved_valuation"])
        self.assertEqual(StockMovement.objects.count(), 2)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 100)

    def test_concurrent_cost_revaluation_retries_once_and_distinct_approvals_need_fresh_preview(self):
        from stock.models import CostRevaluation, ReturnCostReconstruction, ReturnCostRevision, ReturnCostSnapshot, StockCostSnapshot
        product = Product.objects.create(shop=self.shop, name="Recalcul", unit_kind="piece", unit_label="pièce", sale_price_fcfa=100)
        opening = Activity.objects.create(shop=self.shop, actor=self.owner, action="product_created", target_id=product.id, reason="Ouverture")
        StockMovement.objects.create(product=product, actor=self.owner, operation=opening, kind="opening", quantity_milli=2000,
            occurred_at=timezone.now(), reason="Ouverture", unit_cost_micros=1000000000)
        bearer = self.owner_bearer()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
        sale = client.post(f"/api/v1/shops/{self.shop.id}/sales/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
            "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 100}],
        }, format="json")
        self.assertEqual(sale.status_code, 201)
        path = f"/api/v1/shops/{self.shop.id}/products/{product.id}/cost-revaluations/"
        preview = client.get(path + "preview/")
        self.assertEqual(preview.status_code, 200)
        body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Coûts contrôlés",
            "preview_token": preview.data["preview_token"]}
        results = self.simultaneous(path, body, bearer)
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(CostRevaluation.objects.count(), 1)
        self.assertEqual(StockCostSnapshot.objects.count(), 2)
        preview = client.get(path + "preview/")
        first = {**body, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "preview_token": preview.data["preview_token"]}
        second = {**first, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4())}
        results = self.simultaneous(path, first, bearer, second_body=second)
        self.assertEqual(sorted(status for status, _ in results), [201, 409])
        self.assertEqual(CostRevaluation.objects.count(), 2)
        self.assertEqual(StockCostSnapshot.objects.count(), 2)
        self.assertEqual(StockMovement.objects.count(), 2)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 100)
        returns_path = f"/api/v1/shops/{self.shop.id}/sales/{sale.data['id']}/returns/"
        def event(**values):
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "occurred_at": timezone.now().isoformat(), "reason": "Retour contrôlé", **values}
        returned = client.post(returns_path, event(lines=[{"id": str(uuid.uuid4()),
            "sale_line_id": sale.data["lines"][0]["id"], "quantity_milli": 1000}]), format="json")
        self.assertEqual(returned.status_code, 201)
        receipts_path = returns_path + returned.data["id"] + "/lines/" + returned.data["lines"][0]["id"] + "/receipts/"
        receipt = client.post(receipts_path, event(quantity_milli=1000, condition="unsellable"), format="json")
        self.assertEqual(receipt.status_code, 201)
        ReturnCostSnapshot.objects.filter(receipt_id=receipt.data["id"]).delete()  # Legacy receipt fixture.
        preview = client.get(path + "preview/", {"reconstruct_legacy_returns": "true"})
        self.assertEqual(preview.status_code, 200)
        body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Coût ancien contrôlé",
            "preview_token": preview.data["preview_token"], "reconstruct_legacy_returns": True}
        reconstructed = self.simultaneous(path, body, bearer)
        self.assertEqual(sorted(status for status, _ in reconstructed), [200, 201])
        self.assertEqual(reconstructed[0][1], reconstructed[1][1])
        self.assertEqual(ReturnCostReconstruction.objects.count(), 1)
        self.assertEqual(ReturnCostRevision.objects.count(), 1)
        self.assertEqual(CostRevaluation.objects.count(), 3)
        self.assertEqual(StockMovement.objects.count(), 2)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 100)

    def test_concurrent_charge_confirmation_and_reallocation_preserve_one_plan_per_version(self):
        from purchasing.models import Supplier, SupplierDebt, PurchaseCharge, PurchaseChargeRevision, PurchaseChargeAllocation
        products = [Product.objects.create(shop=self.shop, name=name, unit_kind="piece", unit_label="pièce", sale_price_fcfa=1500)
            for name in ("A", "B")]
        supplier = Supplier.objects.create(shop=self.shop, name="Fournisseur")
        bearer = self.owner_bearer()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
        invoice_path = f"/api/v1/shops/{self.shop.id}/suppliers/{supplier.id}/invoices/"
        invoice = client.post(invoice_path, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "occurred_at": timezone.now().isoformat(), "reason": "Facture réelle",
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "quantity_milli": 10000, "unit_price_fcfa": price}
                for product, price in zip(products, (1000, 3000), strict=True)]}, format="json")
        self.assertEqual(invoice.status_code, 201)
        def plan(amounts):
            return [{"line_id": row["id"], "amount_fcfa": amount} for row, amount in zip(invoice.data["lines"], amounts, strict=True)]
        path = invoice_path + invoice.data["id"] + "/charges/"
        body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
            "reason": "Transport confirmé", "label": "Transport", "amount_fcfa": 4000, "allocations": plan((1000, 3000))}
        created = self.simultaneous(path, body, bearer)
        self.assertEqual(sorted(status for status, _ in created), [200, 201])
        self.assertEqual(created[0][1], created[1][1])
        self.assertEqual(PurchaseCharge.objects.count(), 1)
        revisions = path + body["id"] + "/revisions/"
        first = {"operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Colis pesés", "allocations": plan((1500, 2500))}
        second = {**first, "operation_id": str(uuid.uuid4()), "allocations": plan((2000, 2000))}
        changed = self.simultaneous(revisions, first, bearer, second_body=second)
        self.assertEqual(sorted(status for status, _ in changed), [201, 409])
        winner = first if changed[0][0] == 201 else second
        retried = self.simultaneous(revisions, winner, bearer)
        self.assertEqual([status for status, _ in retried], [200, 200])
        self.assertEqual(PurchaseChargeRevision.objects.count(), 2)
        self.assertEqual(PurchaseChargeAllocation.objects.count(), 4)
        self.assertEqual(list(PurchaseChargeAllocation.objects.filter(revision__version=1).order_by("line__position").values_list("amount_fcfa", flat=True)), [1000, 3000])
        self.assertEqual(sum(PurchaseChargeAllocation.objects.filter(revision__version=2).values_list("amount_fcfa", flat=True)), 4000)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 40000)
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

    def test_concurrent_exact_return_receipts_conserve_original_cost_and_retry_without_second_allocation(self):
        from purchasing.models import Supplier
        from stock.models import ReturnCostSnapshot
        product = Product.objects.create(shop=self.shop, name="Fraction", unit_kind="weight", unit_label="kg", sale_price_fcfa=0)
        supplier = Supplier.objects.create(shop=self.shop, name="Fournisseur")
        bearer = self.owner_bearer()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
        def event(**values):
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "occurred_at": timezone.now().isoformat(), "reason": "Fait confirmé", **values}
        invoices = f"/api/v1/shops/{self.shop.id}/suppliers/{supplier.id}/invoices/"
        invoice = client.post(invoices, event(lines=[{"id": str(uuid.uuid4()), "product_id": str(product.id),
            "quantity_milli": 3, "unit_price_fcfa": 249}]), format="json")
        self.assertEqual(invoice.status_code, 201)
        received = client.post(invoices + invoice.data["id"] + "/receipts/", event(lines=[{
            "line_id": invoice.data["lines"][0]["id"], "quantity_milli": 3}]), format="json")
        self.assertEqual(received.status_code, 201)
        sales = f"/api/v1/shops/{self.shop.id}/sales/"
        sale = client.post(sales, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 3}],
            "payments": []}, format="json")
        self.assertEqual(sale.status_code, 201)
        returns = sales + sale.data["id"] + "/returns/"
        returned = client.post(returns, event(lines=[{"id": str(uuid.uuid4()),
            "sale_line_id": sale.data["lines"][0]["id"], "quantity_milli": 3}]), format="json")
        self.assertEqual(returned.status_code, 201)
        receipts = returns + returned.data["id"] + "/lines/" + returned.data["lines"][0]["id"] + "/receipts/"
        first, second = event(quantity_milli=1, condition="unsellable"), event(quantity_milli=1, condition="unsellable")
        results = self.simultaneous(receipts, first, bearer, second_body=second)
        self.assertEqual([status for status, _ in results], [201, 201])
        self.assertEqual(sorted(ReturnCostSnapshot.objects.values_list("total_cost_nanos_fcfa", flat=True)), [333333333, 333333334])
        third = event(quantity_milli=1, condition="unsellable")
        last = client.post(receipts, third, format="json")
        self.assertEqual(last.status_code, 201)
        self.assertEqual(last.data["cost"]["total_cost_nanos_fcfa"], "333333333")
        retries = self.simultaneous(receipts, third, bearer)
        self.assertEqual([status for status, _ in retries], [200, 200])
        self.assertEqual(ReturnCostSnapshot.objects.count(), 3)
        self.assertEqual(sum(ReturnCostSnapshot.objects.values_list("total_cost_nanos_fcfa", flat=True)), 1000000000)
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(StockMovement.objects.filter(kind="customer_return").exists())

    def test_concurrent_known_cost_sales_freeze_each_cost_once_and_keep_oversale_provisional(self):
        from stock.models import StockCostSnapshot
        product = Product.objects.create(shop=self.shop, name="Coût connu", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1500)
        opening = Activity.objects.create(shop=self.shop, actor=self.owner, action="product_created", target_id=product.id, reason="Ouverture")
        StockMovement.objects.create(product=product, actor=self.owner, operation=opening, kind="opening", quantity_milli=10000,
            occurred_at=timezone.now(), reason="Ouverture", unit_cost_micros=1000000000)
        def payload():
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 6000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 9000}]}
        bearer = self.owner_bearer()
        first, second = payload(), payload()
        path = f"/api/v1/shops/{self.shop.id}/sales/"
        results = self.simultaneous(path, first, bearer, second_body=second)
        self.assertEqual([status for status, _ in results], [201, 201])
        costs = StockCostSnapshot.objects.order_by("quantity_after_milli")
        self.assertEqual(costs.count(), 2)
        self.assertEqual(sorted(costs.values_list("cost_state", flat=True)), ["known", "provisional"])
        self.assertEqual(list(costs.values_list("total_cost_nanos_fcfa", flat=True)), [6000000000000, 6000000000000])
        self.assertEqual(list(costs.values_list("quantity_after_milli", flat=True)), [-2000, 4000])
        self.assertEqual(sum(CashMovement.objects.values_list("amount_fcfa", flat=True)), 18000)
        retry = self.simultaneous(path, first, bearer)
        self.assertEqual([status for status, _ in retry], [200, 200])
        self.assertEqual(StockCostSnapshot.objects.count(), 2)
        self.assertEqual(StockMovement.objects.filter(kind="sale").count(), 2)

    def setUp(self):
        self.owner = User.objects.create_user(username="owner")
        self.seller = User.objects.create_user(username="seller")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        Membership.objects.create(shop=self.shop, user=self.seller)
        self.device = Installation.objects.create(user=self.seller)
        self.session = DeviceSession.objects.create(installation=self.device, expires_at=timezone.now() + timedelta(days=1))

    def simultaneous(self, path, body, bearer=None, method="post", second_body=None):
        barrier = Barrier(2, timeout=10)

        def request(index):
            client = APIClient()
            if bearer:
                client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
            try:
                barrier.wait()
                payload = second_body if index == 1 and second_body is not None else body
                response = getattr(client, method)(path, payload, format="json")
                return response.status_code, response.data
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(request, index) for index in range(2)]
            return [future.result(timeout=30) for future in futures]

    def owner_bearer(self):
        device = Installation.objects.create(user=self.owner)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=1))
        token = AccessToken.for_user(self.owner)
        token["session_id"] = str(session.id)
        return str(token)

    def test_concurrent_stock_count_and_confirmation_retries_have_one_observation_and_adjustment(self):
        from stock.models import StockCount, StockCountAdjustment, StockCostSnapshot
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1000)
        bearer = self.owner_bearer()
        root = f"/api/v1/shops/{self.shop.id}/products/{product.id}/stock-counts/"
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "physical_milli": 2000,
                "counted_at": "2026-01-02T10:00:00Z", "reason": "Comptage physique"}
        counts = self.simultaneous(root, data, bearer)
        self.assertEqual(sorted(status for status, _ in counts), [200, 201])
        self.assertEqual(counts[0][1], counts[1][1])
        self.assertEqual(StockCount.objects.count(), 1)
        confirmation = {"operation_id": str(uuid.uuid4()), "expected_milli": 0, "applied_adjustment_milli": 0, "reason": "Écart confirmé"}
        results = self.simultaneous(root + data["id"] + "/confirm/", confirmation, bearer)
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(StockCountAdjustment.objects.count(), 1)
        self.assertEqual(StockMovement.objects.get().quantity_milli, 2000)
        self.assertEqual(StockCostSnapshot.objects.count(), 1)
        self.assertEqual(results[0][1]["cost"]["cost_state"], "unknown")
        self.assertFalse(CashMovement.objects.exists())

    def test_concurrent_distinct_stock_count_confirmations_cannot_apply_the_same_gap_twice(self):
        from stock.models import StockCountAdjustment, StockCostSnapshot
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1000)
        bearer = self.owner_bearer()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
        root = f"/api/v1/shops/{self.shop.id}/products/{product.id}/stock-counts/"
        count = client.post(root, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "physical_milli": 2000,
                                   "counted_at": "2026-01-02T10:00:00Z"}, format="json")
        self.assertEqual(count.status_code, 201)
        first = {"operation_id": str(uuid.uuid4()), "expected_milli": 0, "applied_adjustment_milli": 0, "reason": "Décision A"}
        second = {**first, "operation_id": str(uuid.uuid4()), "reason": "Décision B"}
        results = self.simultaneous(root + count.data["id"] + "/confirm/", first, bearer, second_body=second)
        self.assertEqual(sorted(status for status, _ in results), [201, 409])
        self.assertEqual(StockCountAdjustment.objects.count(), 1)
        self.assertEqual(StockMovement.objects.get().quantity_milli, 2000)
        self.assertEqual(StockCostSnapshot.objects.count(), 1)

    def test_concurrent_category_renames_keep_one_committed_version_and_audit(self):
        from catalogue.models import Category
        category = Category.objects.create(shop=self.shop, name="Initial")
        first = {"operation_id": str(uuid.uuid4()), "base_version": 1, "name": "Premier", "reason": "Correction A"}
        second = {**first, "operation_id": str(uuid.uuid4()), "name": "Deuxième", "reason": "Correction B"}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/categories/{category.id}/", first,
                                    self.owner_bearer(), method="patch", second_body=second)
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(row for status, row in results if status == 200)
        category.refresh_from_db()
        self.assertEqual(category.version, 2)
        self.assertEqual(category.name, winner["name"])
        self.assertEqual(Activity.objects.filter(action="category_updated").count(), 1)

    def test_concurrent_stock_loss_retries_keep_one_physical_effect(self):
        from stock.models import StockCostSnapshot, StockLoss
        product = Product.objects.create(shop=self.shop, name="Savon", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1000)
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-02T00:00:00Z",
                "reason": "Perte physique réelle", "quantity_milli": 2000}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/products/{product.id}/losses/", data, self.owner_bearer())
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(StockLoss.objects.count(), 1)
        self.assertEqual(StockMovement.objects.get().quantity_milli, -2000)
        self.assertTrue(StockLoss.objects.get().stock_requires_regularization)
        self.assertEqual(StockCostSnapshot.objects.count(), 1)
        self.assertEqual(StockCostSnapshot.objects.get().cost_state, "unknown")
        self.assertFalse(CashMovement.objects.exists())

    def test_concurrent_known_cost_losses_keep_each_real_loss_and_one_cost_per_retry(self):
        from stock.models import StockCostSnapshot, StockLoss
        product = Product.objects.create(shop=self.shop, name="Perte valorisée", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1000)
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="product_created", target_id=product.id, reason="Reprise réelle")
        StockMovement.objects.create(product=product, actor=self.owner, operation=event, kind="opening", quantity_milli=3000,
            occurred_at="2026-01-01T00:00:00Z", reason="Reprise réelle", unit_cost_micros=1000000000)
        first = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-02T00:00:00Z",
            "reason": "Casse constatée", "quantity_milli": 2000}
        second = {**first, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4())}
        path = f"/api/v1/shops/{self.shop.id}/products/{product.id}/losses/"
        bearer = self.owner_bearer()
        results = self.simultaneous(path, first, bearer, second_body=second)
        self.assertEqual([status for status, _ in results], [201, 201])
        self.assertEqual(StockLoss.objects.count(), 2)
        self.assertEqual(StockCostSnapshot.objects.count(), 2)
        self.assertEqual(sorted(StockCostSnapshot.objects.values_list("cost_state", flat=True)), ["known", "provisional"])
        self.assertEqual(list(StockCostSnapshot.objects.values_list("total_cost_nanos_fcfa", flat=True)), [2000000000000, 2000000000000])
        self.assertEqual(sum(StockMovement.objects.values_list("quantity_milli", flat=True)), -1000)
        retried = self.simultaneous(path, first, bearer)
        self.assertEqual([status for status, _ in retried], [200, 200])
        self.assertEqual(StockCostSnapshot.objects.count(), 2)
        self.assertEqual(StockMovement.objects.filter(kind="loss").count(), 2)
        self.assertFalse(CashMovement.objects.exists())

    def supplier_invoice_context(self):
        from purchasing.models import Supplier
        supplier = Supplier.objects.create(shop=self.shop, name="Fournisseur")
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1000)
        bearer = self.owner_bearer()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
        root = f"/api/v1/shops/{self.shop.id}/suppliers/{supplier.id}/"
        invoice = client.post(root + "invoices/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-01T00:00:00Z", "reason": "Facture réelle",
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "quantity_milli": 100000, "unit_price_fcfa": 1000}],
        }, format="json")
        self.assertEqual(invoice.status_code, 201)
        return client, bearer, root, invoice.data["debt"]["id"]

    def supplier_advance_payload(self):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-01T00:00:00Z",
                "reason": "Avance réelle", "parts": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 30000}]}

    def supplier_return_context(self):
        client, bearer, root, debt_id = self.supplier_invoice_context()
        invoice = client.get(root + "invoices/").data["results"][0]
        document_url = root + "invoices/" + invoice["id"] + "/"
        for endpoint, quantity, date in (("receipts/", 60000, "2026-01-02T00:00:00Z"), ("returns/", 40000, "2026-01-03T00:00:00Z")):
            response = client.post(document_url + endpoint, {
                "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": date, "reason": "Fait physique confirmé",
                "lines": [{"line_id": invoice["lines"][0]["id"], "quantity_milli": quantity}],
            }, format="json")
            self.assertEqual(response.status_code, 201)
        return client, bearer, root, response.data["id"]

    def supplier_credit_payload(self, return_id):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "return_id": return_id,
                "occurred_at": "2026-01-04T00:00:00Z", "reason": "Avoir accepté", "amount_fcfa": 102000}

    def test_concurrent_supplier_credit_and_refund_retries_have_one_debt_and_cash_effect(self):
        from purchasing.models import SupplierCredit, SupplierDebt, SupplierRefund
        client, bearer, root, return_id = self.supplier_return_context()
        payload = self.supplier_credit_payload(return_id)
        credits = self.simultaneous(root + "credits/", payload, bearer)
        self.assertEqual(sorted(status for status, _ in credits), [200, 201])
        self.assertEqual(credits[0][1], credits[1][1])
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 0)
        self.assertEqual(SupplierCredit.objects.get().remaining_fcfa, 2000)
        refund = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-05T00:00:00Z",
                  "reason": "Remboursement réel", "amount_fcfa": 1500, "method": "cash"}
        refunds = self.simultaneous(root + "credits/" + payload["id"] + "/refund/", refund, bearer)
        self.assertEqual(sorted(status for status, _ in refunds), [200, 201])
        self.assertEqual(refunds[0][1], refunds[1][1])
        self.assertEqual(SupplierRefund.objects.count(), 1)
        self.assertEqual(SupplierCredit.objects.get().remaining_fcfa, 500)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 1500)

    def test_concurrent_distinct_supplier_refunds_cannot_consume_the_same_credit_twice(self):
        from purchasing.models import SupplierCredit, SupplierRefund
        client, bearer, root, return_id = self.supplier_return_context()
        credit = client.post(root + "credits/", self.supplier_credit_payload(return_id), format="json")
        self.assertEqual(credit.status_code, 201)
        first = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-05T00:00:00Z",
                 "reason": "Réception A", "method": "cash", "amount_fcfa": 1500}
        second = {**first, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Réception B"}
        results = self.simultaneous(root + "credits/" + credit.data["id"] + "/refund/", first, bearer, second_body=second)
        self.assertEqual(sorted(status for status, _ in results), [201, 400])
        self.assertEqual(SupplierRefund.objects.count(), 1)
        self.assertEqual(SupplierCredit.objects.get().remaining_fcfa, 500)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 1500)

    def test_concurrent_distinct_supplier_returns_cannot_exceed_actual_receipts(self):
        from stock.models import StockCostSnapshot
        from purchasing.models import SupplierReturn, SupplierDebt
        client, bearer, root, debt_id = self.supplier_invoice_context()
        invoice = client.get(root + "invoices/").data["results"][0]
        document_url = root + "invoices/" + invoice["id"] + "/"
        receipt = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-02T00:00:00Z",
                   "reason": "Quantités reçues", "lines": [{"line_id": invoice["lines"][0]["id"], "quantity_milli": 60000}]}
        self.assertEqual(client.post(document_url + "receipts/", receipt, format="json").status_code, 201)
        first = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-03T00:00:00Z",
                 "reason": "Départ A", "lines": [{"line_id": invoice["lines"][0]["id"], "quantity_milli": 40000}]}
        second = {**first, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Départ B"}
        results = self.simultaneous(document_url + "returns/", first, bearer, second_body=second)
        self.assertEqual(sorted(status for status, _ in results), [201, 400])
        winner = next(value for status, value in results if status == 201)
        self.assertEqual(winner["lines"][0]["cost"]["total_cost_nanos_fcfa"], "40000000000000")
        self.assertEqual(StockCostSnapshot.objects.filter(movement__kind="supplier_return").count(), 1)
        original = first if winner["id"] == first["id"] else second
        retry = client.post(document_url + "returns/", original, format="json")
        self.assertEqual(retry.data, winner)
        self.assertEqual(StockCostSnapshot.objects.filter(movement__kind="supplier_return").count(), 1)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, winner)
        self.assertEqual(SupplierReturn.objects.count(), 1)
        self.assertEqual(sum(StockMovement.objects.values_list("quantity_milli", flat=True)), 20000)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 100000)
        self.assertFalse(CashMovement.objects.exists())

    def test_concurrent_category_creation_retries_keep_one_identity(self):
        from catalogue.models import Category
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Hygiène"}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/categories/", data, self.owner_bearer())
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(Category.objects.count(), 1)
        self.assertEqual(Activity.objects.filter(action="category_created").count(), 1)

    def test_concurrent_supplier_advance_and_allocation_retries_have_one_cash_and_debt_effect(self):
        from purchasing.models import SupplierPayment, SupplierPaymentAllocation, SupplierDebt
        client, bearer, root, debt_id = self.supplier_invoice_context()
        data = self.supplier_advance_payload()
        payments = self.simultaneous(root + "payments/", data, bearer)
        self.assertEqual(sorted(status for status, _ in payments), [200, 201])
        self.assertEqual(payments[0][1], payments[1][1])
        allocation = {"operation_id": str(uuid.uuid4()), "reason": "Affectation réelle",
                      "allocations": [{"debt_id": debt_id, "amount_fcfa": 30000}]}
        allocations = self.simultaneous(root + "payments/" + data["id"] + "/allocate/", allocation, bearer)
        self.assertEqual(sorted(status for status, _ in allocations), [200, 201])
        self.assertEqual(allocations[0][1], allocations[1][1])
        self.assertEqual(SupplierPayment.objects.get().unallocated_fcfa, 0)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 70000)
        self.assertEqual(SupplierPaymentAllocation.objects.count(), 1)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, -30000)

    def test_concurrent_distinct_allocations_cannot_spend_the_same_supplier_advance_twice(self):
        from purchasing.models import SupplierPaymentAllocation, SupplierDebt
        client, bearer, root, debt_id = self.supplier_invoice_context()
        data = self.supplier_advance_payload()
        self.assertEqual(client.post(root + "payments/", data, format="json").status_code, 201)
        first = {"operation_id": str(uuid.uuid4()), "reason": "Affectation A", "allocations": [{"debt_id": debt_id, "amount_fcfa": 30000}]}
        second = {**first, "operation_id": str(uuid.uuid4()), "reason": "Affectation B"}
        results = self.simultaneous(root + "payments/" + data["id"] + "/allocate/", first, bearer, second_body=second)
        self.assertEqual(sorted(status for status, _ in results), [201, 400])
        self.assertEqual(SupplierPaymentAllocation.objects.count(), 1)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 70000)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, -30000)

    def test_concurrent_invoice_and_receipt_retries_have_one_debt_and_one_stock_effect(self):
        from purchasing.models import Supplier, SupplierDebt, PurchaseReceipt
        supplier = Supplier.objects.create(shop=self.shop, name="Fournisseur")
        product = Product.objects.create(shop=self.shop, name="Savon", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1000)
        bearer = self.owner_bearer()
        url = f"/api/v1/shops/{self.shop.id}/suppliers/{supplier.id}/invoices/"
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-01T00:00:00Z",
                "reason": "Facture réelle", "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id),
                                                        "quantity_milli": 10000, "unit_price_fcfa": 1000}]}
        invoices = self.simultaneous(url, data, bearer)
        self.assertEqual(sorted(status for status, _ in invoices), [200, 201])
        self.assertEqual(invoices[0][1], invoices[1][1])
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 10000)
        receipt = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-02T00:00:00Z",
                   "reason": "Réception physique", "lines": [{"line_id": data["lines"][0]["id"], "quantity_milli": 6000}]}
        receipts = self.simultaneous(url + data["id"] + "/receipts/", receipt, bearer)
        self.assertEqual(sorted(status for status, _ in receipts), [200, 201])
        self.assertEqual(receipts[0][1], receipts[1][1])
        self.assertEqual(PurchaseReceipt.objects.count(), 1)
        self.assertEqual(StockMovement.objects.get().quantity_milli, 6000)
        self.assertFalse(CashMovement.objects.exists())

    def test_concurrent_manual_cash_retry_has_one_event_and_amount(self):
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "kind": "expense", "method": "cash",
                "amount_fcfa": 2000, "occurred_at": "2026-01-01T00:00:00Z", "reason": "Achat matériel déclaré une fois"}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/cash/", data, self.owner_bearer())
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(CashMovement.objects.get().amount_fcfa, -2000)
        self.assertEqual(Activity.objects.filter(action="cash_manual_created").count(), 1)

    def test_concurrent_physical_count_retry_preserves_one_observation_without_cash(self):
        from cash.models import CashCount
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "method": "cash", "physical_fcfa": 48000,
                "counted_at": "2026-01-01T00:00:00Z", "reason": "Comptage déclaré une fois"}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/cash-counts/", data, self.owner_bearer())
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(CashCount.objects.get().physical_fcfa, 48000)
        self.assertFalse(CashMovement.objects.exists())
        self.assertEqual(Activity.objects.filter(action="cash_count_created").count(), 1)

    def test_concurrent_unreturned_change_retry_has_one_obligation_and_refund(self):
        bearer = self.owner_bearer()
        customer = Customer.objects.create(shop=self.shop, name="Monnaie due")
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=7500)
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "customer_id": str(customer.id),
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 10000,
                              "received_fcfa": 10000, "returned_fcfa": 0}]}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/sales/", data, bearer)
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        credit = CustomerCredit.objects.get()
        self.assertEqual(credit.remaining_fcfa, 2500)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 10000)
        refunds = self.simultaneous(
            f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/credits/{credit.id}/refund/",
            {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 2500,
             "method": "cash", "reason": "Monnaie rendue une fois"}, bearer,
        )
        self.assertEqual(sorted(status for status, _ in refunds), [200, 201])
        self.assertEqual(refunds[0][1], refunds[1][1])
        credit.refresh_from_db()
        self.assertEqual(credit.remaining_fcfa, 0)
        self.assertEqual(CashMovement.objects.filter(amount_fcfa=-2500).count(), 1)

    def test_concurrent_same_external_reference_keeps_both_real_payments(self):
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=7500)
        def data():
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                    "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
                    "payments": [{"id": str(uuid.uuid4()), "method": "orange", "amount_fcfa": 7500,
                                  "external_reference": "DUPLICATE-REAL"}]}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/sales/", data(), self.owner_bearer(), second_body=data())
        self.assertEqual([status for status, _ in results], [201, 201])
        self.assertEqual(sorted(row[1]["payments"][0]["possible_duplicate_reference"] for row in results), [False, True])
        self.assertEqual(CashMovement.objects.count(), 2)
        self.assertEqual(sum(CashMovement.objects.values_list("amount_fcfa", flat=True)), 15000)

    def test_concurrent_rights_changes_do_not_overwrite_same_version(self):
        member = Membership.objects.get(shop=self.shop, user=self.seller)
        first = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                 "reason": "Crédit autorisé", "can_grant_credit": True}
        second = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                  "reason": "Coûts autorisés", "can_read_cost": True}
        results = self.simultaneous(
            f"/api/v1/shops/{self.shop.id}/team/{member.id}/", first,
            self.owner_bearer(), method="patch", second_body=second,
        )
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        member.refresh_from_db()
        self.assertEqual(member.version, 2)
        self.assertNotEqual(member.can_grant_credit, member.can_read_cost)
        self.assertEqual(Activity.objects.filter(action="membership_changed").count(), 1)

    def test_concurrent_rights_retry_returns_same_receipt_once(self):
        member = Membership.objects.get(shop=self.shop, user=self.seller)
        operation = uuid.uuid4()
        results = self.simultaneous(
            f"/api/v1/shops/{self.shop.id}/team/{member.id}/",
            {"operation_id": str(operation), "base_version": 1,
             "reason": "Crédit autorisé", "can_grant_credit": True},
            self.owner_bearer(), method="patch",
        )
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        member.refresh_from_db()
        self.assertEqual(member.version, 2)
        self.assertEqual(Activity.objects.filter(id=operation).count(), 1)

    def test_concurrent_product_updates_keep_one_confirmed_version(self):
        product = Product.objects.create(shop=self.shop, name="Bouteille", unit_kind="piece", unit_label="bouteille", sale_price_fcfa=250)
        first = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                 "reason": "Prix A", "sale_price_fcfa": 300}
        second = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                  "reason": "Prix B", "sale_price_fcfa": 350}
        results = self.simultaneous(
            f"/api/v1/shops/{self.shop.id}/products/{product.id}/", first,
            self.owner_bearer(), method="patch", second_body=second,
        )
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(data for status, data in results if status == 200)
        product.refresh_from_db()
        self.assertEqual(product.version, 2)
        self.assertEqual(product.sale_price_fcfa, winner["sale_price_fcfa"])
        audit = Activity.objects.get(action="product_updated", target_id=product.id)
        self.assertEqual(audit.details["before"]["sale_price_fcfa"], 250)
        self.assertEqual(audit.details["after"]["sale_price_fcfa"], product.sale_price_fcfa)
        self.assertFalse(StockMovement.objects.exists())

    def test_concurrent_product_update_retry_has_one_audit(self):
        product = Product.objects.create(shop=self.shop, name="Bouteille", unit_kind="piece", unit_label="bouteille", sale_price_fcfa=250)
        operation = uuid.uuid4()
        results = self.simultaneous(
            f"/api/v1/shops/{self.shop.id}/products/{product.id}/",
            {"operation_id": str(operation), "base_version": 1,
             "reason": "Nouveau prix", "sale_price_fcfa": 300},
            self.owner_bearer(), method="patch",
        )
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        product.refresh_from_db()
        self.assertEqual(product.version, 2)
        self.assertEqual(Activity.objects.filter(id=operation).count(), 1)

    def test_concurrent_sale_retry_has_one_stock_and_cash_effect(self):
        product = Product.objects.create(shop=self.shop, name="Bouteille", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1000)
        operation = uuid.uuid4()
        results = self.simultaneous(
            f"/api/v1/shops/{self.shop.id}/sales/", {
                "id": str(uuid.uuid4()), "operation_id": str(operation),
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000}],
            }, self.owner_bearer(),
        )
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(SalePayment.objects.count(), 1)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 1000)
        self.assertEqual(StockMovement.objects.get().quantity_milli, -1000)
        self.assertEqual(Activity.objects.filter(id=operation).count(), 1)

    def test_concurrent_real_sales_keep_both_payments_and_negative_stock(self):
        product = Product.objects.create(shop=self.shop, name="Bouteille", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1000)
        def payload():
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                    "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
                    "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000}]}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/sales/", payload(),
                                    self.owner_bearer(), second_body=payload())
        self.assertEqual([status for status, _ in results], [201, 201])
        self.assertEqual(Sale.objects.count(), 2)
        self.assertEqual(CashMovement.objects.count(), 2)
        self.assertEqual(sum(CashMovement.objects.values_list("amount_fcfa", flat=True)), 2000)
        self.assertEqual(sum(StockMovement.objects.values_list("quantity_milli", flat=True)), -2000)
        self.assertTrue(all(data["stock_requires_regularization"] for _, data in results))

    def test_concurrent_discount_policy_changes_keep_one_version(self):
        first = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                 "reason": "Plafond A", "seller_discount_cap_basis_points": 500}
        second = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                  "reason": "Plafond B", "seller_discount_cap_basis_points": 1000}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/discount-policy/", first,
                                    self.owner_bearer(), second_body=second)
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(data for status, data in results if status == 200)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.discount_policy_version, 2)
        self.assertEqual(self.shop.seller_discount_cap_basis_points, winner["seller_discount_cap_basis_points"])
        self.assertEqual(Activity.objects.filter(action="discount_policy_changed").count(), 1)

    def test_concurrent_discount_policy_retry_returns_same_receipt(self):
        operation = uuid.uuid4()
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/discount-policy/", {
            "operation_id": str(operation), "base_version": 1,
            "reason": "Plafond confirmé", "seller_discount_cap_basis_points": 500,
        }, self.owner_bearer())
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.discount_policy_version, 2)
        self.assertEqual(Activity.objects.filter(id=operation).count(), 1)

    def test_two_concurrent_sales_of_last_unit_end_at_minus_one(self):
        product = Product.objects.create(shop=self.shop, name="Dernière unité", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1000)
        opening = Activity.objects.create(shop=self.shop, actor=self.owner, action="product_created", target_id=product.id, reason="Ouverture")
        StockMovement.objects.create(product=product, actor=self.owner, operation=opening, kind="opening", quantity_milli=1000,
                                     occurred_at=timezone.now(), reason="Ouverture", unit_cost_micros=None)
        def payload():
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                    "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
                    "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000}]}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/sales/", payload(),
                                    self.owner_bearer(), second_body=payload())
        self.assertEqual([status for status, _ in results], [201, 201])
        self.assertEqual(sum(StockMovement.objects.values_list("quantity_milli", flat=True)), -1000)
        self.assertEqual(sum(CashMovement.objects.values_list("amount_fcfa", flat=True)), 2000)
        self.assertEqual(Sale.objects.count(), 2)
        self.assertEqual(sorted(data["stock_requires_regularization"] for _, data in results), [False, True])

    def test_concurrent_credit_retry_creates_one_debt_without_cash(self):
        customer = Customer.objects.create(shop=self.shop, name="Client crédit")
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=10000)
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "mode": "credit", "customer_id": str(customer.id),
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
                "payments": []}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/sales/", data, self.owner_bearer())
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(CustomerDebt.objects.get().remaining_fcfa, 10000)
        self.assertFalse(CashMovement.objects.exists())
        self.assertEqual(StockMovement.objects.get().quantity_milli, -1000)

    def test_concurrent_new_credit_rechecks_balance_before_second_live_sale(self):
        customer = Customer.objects.create(shop=self.shop, name="Client crédit")
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=10000)
        def payload():
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "mode": "credit", "customer_id": str(customer.id),
                    "confirmed_debt_fcfa": 0,
                    "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
                    "payments": []}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/sales/", payload(),
                                    self.owner_bearer(), second_body=payload())
        self.assertEqual(sorted(status for status, _ in results), [201, 409])
        rejected = next(data for status, data in results if status == 409)
        self.assertEqual(rejected["known_debt_fcfa"], 10000)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(CustomerDebt.objects.count(), 1)
        self.assertFalse(CashMovement.objects.exists())

    def debt_fixture(self):
        customer = Customer.objects.create(shop=self.shop, name="Client encaissement")
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="test_opening_debt", target_id=customer.id, reason="Fixture")
        debt = CustomerDebt.objects.create(shop=self.shop, customer=customer, actor=self.owner,
                                          installation=self.device, operation=event, amount_fcfa=10000,
                                          remaining_fcfa=10000, occurred_at=timezone.now())
        return customer, debt

    def test_concurrent_collection_retry_affects_debt_and_cash_once(self):
        customer, debt = self.debt_fixture()
        operation = uuid.uuid4()
        body = {"id": str(uuid.uuid4()), "operation_id": str(operation),
                "parts": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 10000}],
                "allocations": [{"id": str(uuid.uuid4()), "debt_id": str(debt.id), "amount_fcfa": 10000}]}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/payments/", body, self.owner_bearer())
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        debt.refresh_from_db()
        self.assertEqual(debt.remaining_fcfa, 0)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 10000)
        self.assertEqual(Activity.objects.filter(id=operation).count(), 1)

    def test_concurrent_real_collections_keep_20000_and_flag_10000_for_regularization(self):
        customer, debt = self.debt_fixture()
        def payload():
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                    "parts": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 10000}],
                    "allocations": [{"id": str(uuid.uuid4()), "debt_id": str(debt.id), "amount_fcfa": 10000}]}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/payments/", payload(),
                                    self.owner_bearer(), second_body=payload())
        self.assertEqual([status for status, _ in results], [201, 201])
        self.assertEqual(sorted(data["unallocated_fcfa"] for _, data in results), [0, 10000])
        self.assertEqual(sorted(data["requires_regularization"] for _, data in results), [False, True])
        self.assertEqual(sum(CashMovement.objects.values_list("amount_fcfa", flat=True)), 20000)
        self.assertEqual(CashMovement.objects.count(), 2)
        debt.refresh_from_db()
        self.assertEqual(debt.remaining_fcfa, 0)

    def unallocated_fixture(self):
        customer, debt = self.debt_fixture()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.owner_bearer()}")
        body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "parts": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 5000}], "allocations": []}
        response = client.post(f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/payments/", body, format="json")
        self.assertEqual(response.status_code, 201)
        return customer, debt, CustomerPayment.objects.get(id=body["id"])

    def test_concurrent_refund_retry_has_one_negative_cash_effect(self):
        customer, _, payment = self.unallocated_fixture()
        operation = uuid.uuid4()
        results = self.simultaneous(
            f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/payments/{payment.id}/refund/", {
                "id": str(uuid.uuid4()), "operation_id": str(operation), "reason": "Argent rendu",
                "amount_fcfa": 5000, "method": "cash",
            }, self.owner_bearer(),
        )
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(sorted(CashMovement.objects.values_list("amount_fcfa", flat=True)), [-5000, 5000])
        self.assertEqual(PaymentRegularization.objects.count(), 1)
        payment.refresh_from_db()
        self.assertEqual(payment.unallocated_fcfa, 0)

    def test_concurrent_allocations_cannot_spend_same_unallocated_money_twice(self):
        customer, debt, payment = self.unallocated_fixture()
        def body():
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Affectation",
                    "allocations": [{"id": str(uuid.uuid4()), "debt_id": str(debt.id), "amount_fcfa": 5000}]}
        results = self.simultaneous(
            f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/payments/{payment.id}/allocate/",
            body(), self.owner_bearer(), second_body=body(),
        )
        self.assertEqual(sorted(status for status, _ in results), [201, 409])
        self.assertEqual(PaymentRegularization.objects.count(), 1)
        self.assertEqual(CashMovement.objects.count(), 1)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 5000)
        payment.refresh_from_db()
        debt.refresh_from_db()
        self.assertEqual(payment.unallocated_fcfa, 0)
        self.assertEqual(debt.remaining_fcfa, 5000)

    def test_concurrent_opening_debt_retry_creates_one_receivable_without_cash(self):
        customer = Customer.objects.create(shop=self.shop, name="Solde antérieur")
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 10000,
                "occurred_at": "2026-01-01T00:00:00Z", "reason": "Solde confirmé"}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/debts/", data, self.owner_bearer())
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(CustomerDebt.objects.count(), 1)
        self.assertEqual(CustomerDebt.objects.get().remaining_fcfa, 10000)
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(Sale.objects.exists())

    def test_concurrent_deadline_changes_keep_one_due_version(self):
        customer, debt = self.debt_fixture()
        first = {"operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Accord A", "due_date": "2026-10-31"}
        second = {"operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Accord B", "due_date": "2026-11-30"}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/debts/{debt.id}/due-date/", first,
                                    self.owner_bearer(), second_body=second)
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(data for status, data in results if status == 200)
        debt.refresh_from_db()
        self.assertEqual(debt.due_version, 2)
        self.assertEqual(debt.due_at.isoformat().replace("+00:00", "Z"), winner["due_at"])
        self.assertEqual(debt.remaining_fcfa, 10000)
        self.assertEqual(Activity.objects.filter(action="customer_debt_due_changed").count(), 1)
        self.assertFalse(CashMovement.objects.exists())

    def test_same_refresh_has_exactly_one_successful_rotation(self):
        refresh = RefreshToken.for_user(self.seller)
        refresh["session_id"] = str(self.session.id)
        self.session.refresh_jti = refresh["jti"]
        self.session.save()
        results = self.simultaneous("/api/v1/auth/refresh/", {"refresh": str(refresh)})
        self.assertEqual(sorted(status for status, _ in results), [200, 401])
        pair = next(data for status, data in results if status == 200)
        replacement = RefreshToken(pair["refresh"])
        self.session.refresh_from_db()
        self.assertEqual(self.session.refresh_jti, replacement["jti"])
        client = APIClient()
        self.assertEqual(client.post("/api/v1/auth/refresh/", {"refresh": pair["refresh"]}, format="json").status_code, 200)

    def test_simultaneous_revoke_retries_have_one_audit_and_same_receipt(self):
        owner_device = Installation.objects.create(user=self.owner)
        session = DeviceSession.objects.create(installation=owner_device, expires_at=timezone.now() + timedelta(days=1))
        access = AccessToken.for_user(self.owner)
        access["session_id"] = str(session.id)
        operation_id = uuid.uuid4()
        results = self.simultaneous(
            f"/api/v1/shops/{self.shop.id}/revoke-device/",
            {"operation_id": str(operation_id), "installation_id": str(self.device.id), "reason": "Appareil perdu"},
            str(access),
        )
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(Activity.objects.filter(id=operation_id).count(), 1)

    def test_simultaneous_product_retry_creates_only_one_opening_movement(self):
        device = Installation.objects.create(user=self.owner)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=1))
        access = AccessToken.for_user(self.owner)
        access["session_id"] = str(session.id)
        product_id, operation_id = uuid.uuid4(), uuid.uuid4()
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/products/", {
            "id": str(product_id), "operation_id": str(operation_id), "name": "Savon",
            "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 1000,
            "opening": {"quantity_milli": 10000, "unit_cost_fcfa": None,
                        "occurred_at": "2026-01-01T00:00:00Z", "reason": "Reprise"},
        }, str(access))
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(Product.objects.filter(id=product_id).count(), 1)
        self.assertEqual(StockMovement.objects.filter(product_id=product_id).count(), 1)
        self.assertEqual(StockMovement.objects.get(product_id=product_id).quantity_milli, 10000)

    def test_simultaneous_pack_retry_creates_one_pack_and_audit(self):
        product = Product.objects.create(shop=self.shop, name="Bouteille", unit_kind="piece", unit_label="bouteille", sale_price_fcfa=250)
        device = Installation.objects.create(user=self.owner)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=1))
        access = AccessToken.for_user(self.owner)
        access["session_id"] = str(session.id)
        pack_id, operation_id = uuid.uuid4(), uuid.uuid4()
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/products/{product.id}/packs/", {
            "id": str(pack_id), "operation_id": str(operation_id), "name": "Carton",
            "base_quantity_milli": 24000, "sale_price_fcfa": 4800,
        }, str(access))
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(ProductPack.objects.filter(id=pack_id).count(), 1)
        self.assertEqual(Activity.objects.filter(id=operation_id).count(), 1)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_concurrent_client_corrections_keep_first_committed_version(self):
        customer = Customer.objects.create(shop=self.shop, name="Original")
        device = Installation.objects.create(user=self.owner)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=1))
        access = AccessToken.for_user(self.owner)
        access["session_id"] = str(session.id)
        first = {"operation_id": str(uuid.uuid4()), "base_version": 1, "name": "Premier", "reason": "Correction A"}
        second = {"operation_id": str(uuid.uuid4()), "base_version": 1, "name": "Deuxième", "reason": "Correction B"}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/", first,
                                    str(access), method="patch", second_body=second)
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(data for status, data in results if status == 200)
        customer.refresh_from_db()
        self.assertEqual(customer.version, 2)
        self.assertEqual(customer.name, winner["name"])
        audit = Activity.objects.get(action="client_updated", target_id=customer.id)
        self.assertEqual(audit.details["before"]["name"], "Original")
        self.assertEqual(audit.details["after"]["name"], winner["name"])

    def test_concurrent_stock_threshold_retries_preserve_one_version_and_receipt(self):
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=250)
        data = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                "reason": "Seuil responsable", "stock_threshold_milli": 8000}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/products/{product.id}/", data,
                                    self.owner_bearer(), method="patch")
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        product.refresh_from_db()
        self.assertEqual(product.version, 2)
        self.assertEqual(product.stock_threshold_milli, 8000)
        self.assertEqual(Activity.objects.filter(action="product_updated").count(), 1)

    def test_concurrent_distinct_stock_threshold_changes_require_version_resolution(self):
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=250)
        first = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                 "reason": "Seuil A", "stock_threshold_milli": 8000}
        second = {**first, "operation_id": str(uuid.uuid4()), "stock_threshold_milli": 9000}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/products/{product.id}/", first,
                                    self.owner_bearer(), method="patch", second_body=second)
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        product.refresh_from_db()
        winner = next(body for status, body in results if status == 200)
        self.assertEqual(product.stock_threshold_milli, winner["stock_threshold_milli"])
        self.assertEqual(product.version, 2)
        self.assertEqual(Activity.objects.filter(action="product_updated").count(), 1)

    def test_concurrent_sales_keep_rejected_departure_reserved_without_a_second_episode(self):
        from stock.models import StockAlertEvent, StockReservation
        from stock.services.stock_alert_services import observe_stock
        from stock.services.stock_queries import with_stock
        product = Product.objects.create(shop=self.shop, name="Refus à régulariser", unit_kind="piece", unit_label="pièce",
            sale_price_fcfa=250, stock_threshold_milli=8000)
        opening = Activity.objects.create(shop=self.shop, actor=self.owner, action="stock_fixture",
            target_id=product.id, reason="Ouverture")
        StockMovement.objects.create(product=product, actor=self.owner, operation=opening, kind="opening",
            quantity_milli=10000, occurred_at=timezone.now(), reason="Ouverture")
        rejected = Activity.objects.create(shop=self.shop, actor=self.owner, action="rejected_sale_fixture",
            target_id=product.id, reason="Départ connu non régularisé")
        StockReservation.objects.create(product=product, operation=rejected, state="rejected", quantity_milli=2000)
        observe_stock([product.id], rejected)
        def payload():
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 250}]}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/sales/", payload(), self.owner_bearer(), second_body=payload())
        self.assertEqual([status for status, _ in results], [201, 201])
        current = with_stock(Product.objects.filter(id=product.id)).get()
        self.assertEqual(int(current.stock_validated_milli), 8000)
        self.assertEqual(int(current.stock_reserved_milli), 2000)
        self.assertEqual(int(current.stock_rejected_reserved_milli), 2000)
        self.assertEqual(int(current.stock_available_milli), 6000)
        self.assertEqual(StockAlertEvent.objects.count(), 1)
        self.assertEqual(StockAlertEvent.objects.get().operation_id, rejected.id)
        self.assertEqual(StockMovement.objects.filter(kind="sale").count(), 2)
        self.assertEqual(sum(CashMovement.objects.values_list("amount_fcfa", flat=True)), 500)

    def test_concurrent_sales_stock_crossing_has_one_durable_episode(self):
        from stock.models import StockAlertEvent, StockAlertState
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce",
                                         sale_price_fcfa=250, stock_threshold_milli=8000)
        opening = Activity.objects.create(shop=self.shop, actor=self.owner, action="stock_fixture",
                                          target_id=product.id, reason="Ouverture")
        StockMovement.objects.create(product=product, actor=self.owner, operation=opening, kind="opening",
                                     quantity_milli=10000, occurred_at=timezone.now(), reason="Ouverture")
        def payload():
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                    "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
                    "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 250}]}
        first, second = payload(), payload()
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/sales/", first,
                                    self.owner_bearer(), second_body=second)
        self.assertEqual([status for status, _ in results], [201, 201])
        self.assertEqual(StockAlertEvent.objects.count(), 1)
        self.assertEqual(StockAlertEvent.objects.get().available_milli, 8000)
        self.assertEqual(StockAlertState.objects.get().episode, 1)
        self.assertEqual(StockMovement.objects.filter(kind="sale").count(), 2)

    def test_concurrent_duplicate_barcode_product_creation_has_one_product_and_stock_effect(self):
        from catalogue.models import Barcode
        def body():
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Produit",
                    "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 250, "barcode": "DUPLICATE",
                    "opening": {"quantity_milli": 10000, "unit_cost_fcfa": None,
                                "occurred_at": "2026-01-01T10:00:00Z", "reason": "Ouverture"}}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/products/", body(),
                                    self.owner_bearer(), second_body=body())
        self.assertEqual(sorted(status for status, _ in results), [201, 409])
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(Barcode.objects.count(), 1)
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertEqual(StockMovement.objects.get().quantity_milli, 10000)

    def test_concurrent_barcode_assignment_retries_keep_one_registration_and_audit(self):
        from catalogue.models import Barcode
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=250)
        data = {"operation_id": str(uuid.uuid4()), "base_version": 0, "code": "0012345678905", "reason": "Code vérifié"}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/products/{product.id}/barcode/", data,
                                    self.owner_bearer(), method="patch")
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(Barcode.objects.get().version, 1)
        self.assertEqual(Activity.objects.filter(action="barcode_assigned").count(), 1)

    def test_concurrent_distinct_barcode_changes_cannot_overwrite_each_other(self):
        from catalogue.models import Barcode
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=250)
        first = {"operation_id": str(uuid.uuid4()), "base_version": 0, "code": "A", "reason": "Code A"}
        second = {**first, "operation_id": str(uuid.uuid4()), "code": "B"}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/products/{product.id}/barcode/", first,
                                    self.owner_bearer(), method="patch", second_body=second)
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(body for status, body in results if status == 200)
        self.assertEqual(Barcode.objects.get().code, winner["code"])
        self.assertEqual(Barcode.objects.get().version, 1)

    def photo_requests(self, product, operation_ids, colors):
        from io import BytesIO
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        bearer = self.owner_bearer()
        barrier = Barrier(2, timeout=10)
        def request(index):
            output = BytesIO()
            Image.new("RGB", (64, 48), colors[index]).save(output, format="JPEG")
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
            body = {"operation_id": operation_ids[index], "base_version": 0, "reason": "Photo concurrente",
                    "image": SimpleUploadedFile("phone.jpg", output.getvalue(), content_type="image/jpeg")}
            try:
                barrier.wait()
                response = client.post(f"/api/v1/shops/{self.shop.id}/products/{product.id}/photo/", body, format="multipart")
                return response.status_code, response.data
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(request, index) for index in range(2)]
            return [future.result(timeout=30) for future in futures]

    def test_concurrent_photo_retries_keep_one_blob_version_and_event(self):
        from catalogue.models import ProductPhoto
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=250)
        operation = str(uuid.uuid4())
        results = self.photo_requests(product, [operation, operation], ["blue", "blue"])
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(ProductPhoto.objects.count(), 1)
        self.assertEqual(ProductPhoto.objects.get().version, 1)
        self.assertLessEqual(ProductPhoto.objects.get().size_bytes, 20000)
        self.assertEqual(Activity.objects.filter(action="product_photo_updated").count(), 1)

    def test_concurrent_distinct_photos_cannot_overwrite_the_same_version(self):
        from catalogue.models import ProductPhoto
        import hashlib
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=250)
        results = self.photo_requests(product, [str(uuid.uuid4()), str(uuid.uuid4())], ["blue", "red"])
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(body for status, body in results if status == 200)
        stored = ProductPhoto.objects.get()
        self.assertEqual(hashlib.sha256(bytes(stored.data)).hexdigest(), winner["sha256"])
        self.assertEqual(stored.version, 1)

    def test_concurrent_language_retries_return_one_durable_change(self):
        from identity.models import LanguagePreferenceChange
        data = {"operation_id": str(uuid.uuid4()), "base_version": 1, "language": "en"}
        results = self.simultaneous("/api/v1/account/language/", data, self.owner_bearer(), method="patch")
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.language, "en")
        self.assertEqual(self.owner.language_version, 2)
        self.assertEqual(LanguagePreferenceChange.objects.count(), 1)

    def test_concurrent_distinct_language_choices_preserve_one_winner(self):
        from identity.models import LanguagePreferenceChange
        data = {"operation_id": str(uuid.uuid4()), "base_version": 1, "language": "en"}
        second = {**data, "operation_id": str(uuid.uuid4()), "language": "fr"}
        results = self.simultaneous("/api/v1/account/language/", data, self.owner_bearer(), method="patch", second_body=second)
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(body for status, body in results if status == 200)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.language, winner["language"])
        self.assertEqual(self.owner.language_version, 2)
        self.assertEqual(LanguagePreferenceChange.objects.count(), 1)

    def test_concurrent_pack_change_retries_advance_catalogue_only_once(self):
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=250)
        pack = ProductPack.objects.create(product=product, name="Carton", base_quantity_milli=24000, sale_price_fcfa=4800)
        data = {"operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Tarif confirmé", "sale_price_fcfa": 4600}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/products/{product.id}/packs/{pack.id}/", data,
                                    self.owner_bearer(), method="patch")
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        pack.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual((pack.version, product.version), (2, 2))
        self.assertEqual(Activity.objects.filter(action="pack_updated").count(), 1)
        self.assertFalse(StockMovement.objects.exists())

    def test_concurrent_distinct_pack_edits_preserve_one_catalogue_winner(self):
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=250)
        pack = ProductPack.objects.create(product=product, name="Carton", base_quantity_milli=24000, sale_price_fcfa=4800)
        data = {"operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Décision A", "base_quantity_milli": 12000}
        second = {**data, "operation_id": str(uuid.uuid4()), "reason": "Décision B", "base_quantity_milli": 6000}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/products/{product.id}/packs/{pack.id}/", data,
                                    self.owner_bearer(), method="patch", second_body=second)
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(body for status, body in results if status == 200)
        pack.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(pack.base_quantity_milli, winner["base_quantity_milli"])
        self.assertEqual((pack.version, product.version), (2, 2))
        self.assertEqual(Activity.objects.filter(action="pack_updated").count(), 1)

    def import_confirmation_fixture(self):
        import hashlib
        from catalogue.models import CatalogueImport
        bearer = self.owner_bearer()
        installation = DeviceSession.objects.get(installation__user=self.owner).installation
        source = (b"name,unit_kind,unit_label,sale_price_fcfa,stock_threshold_milli,opening_quantity_milli,opening_unit_cost_fcfa,opening_occurred_at,opening_reason\n"
                  b"Savon,piece,piece,250,8000,5000,,2026-01-01T10:00:00Z,Stock compte\n")
        batch = CatalogueImport.objects.create(shop=self.shop, actor=self.owner, installation=installation,
            source=source, source_sha256=hashlib.sha256(source).hexdigest())
        data = {"operation_id": str(uuid.uuid4()), "source_sha256": batch.source_sha256, "reason": "Import confirmé"}
        return batch, data, bearer

    def test_concurrent_import_confirmation_retries_create_one_stock_and_episode(self):
        from catalogue.models import CatalogueImport
        from stock.models import StockAlertEvent
        batch, data, bearer = self.import_confirmation_fixture()
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/catalogue-imports/{batch.id}/confirm/", data, bearer)
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(StockMovement.objects.get().quantity_milli, 5000)
        self.assertEqual(StockAlertEvent.objects.count(), 1)
        self.assertEqual(Activity.objects.filter(action="catalogue_import_applied").count(), 1)
        self.assertIsNotNone(CatalogueImport.objects.get().applied_operation_id)

    def test_concurrent_distinct_import_confirmations_cannot_apply_file_twice(self):
        from stock.models import StockAlertEvent
        batch, data, bearer = self.import_confirmation_fixture()
        second = {**data, "operation_id": str(uuid.uuid4())}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/catalogue-imports/{batch.id}/confirm/", data, bearer,
                                    second_body=second)
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertEqual(StockAlertEvent.objects.count(), 1)
        self.assertEqual(Activity.objects.filter(action="catalogue_import_applied").count(), 1)

    def party_import_confirmation_fixture(self, kind):
        import hashlib
        from catalogue.models import CatalogueImport
        bearer = self.owner_bearer()
        installation = DeviceSession.objects.get(installation__user=self.owner).installation
        source = b"name,phone,opening_debt_fcfa,opening_occurred_at,opening_reason\nA,,3000,2026-01-01T10:00:00Z,Solde\n"
        batch = CatalogueImport.objects.create(kind=kind, shop=self.shop, actor=self.owner, installation=installation,
            source=source, source_sha256=hashlib.sha256(source).hexdigest())
        data = {"operation_id": str(uuid.uuid4()), "source_sha256": batch.source_sha256, "reason": "Soldes vérifiés"}
        prefix = "customer-imports" if kind == "customers" else "supplier-imports"
        return f"/api/v1/shops/{self.shop.id}/{prefix}/{batch.id}/confirm/", data, bearer

    def test_concurrent_customer_import_retries_keep_one_party_debt_and_ledger_entry(self):
        from customers.models import CustomerDebt, CustomerLedgerEntry
        path, data, bearer = self.party_import_confirmation_fixture("customers")
        results = self.simultaneous(path, data, bearer)
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(CustomerDebt.objects.get().amount_fcfa, 3000)
        self.assertEqual(CustomerLedgerEntry.objects.count(), 1)
        self.assertFalse(CashMovement.objects.exists())

    def test_concurrent_distinct_supplier_import_confirmations_keep_one_party_and_debt(self):
        from purchasing.models import Supplier, SupplierDebt
        path, data, bearer = self.party_import_confirmation_fixture("suppliers")
        results = self.simultaneous(path, data, bearer, second_body={**data, "operation_id": str(uuid.uuid4())})
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        self.assertEqual(Supplier.objects.count(), 1)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 3000)
        self.assertFalse(CashMovement.objects.exists())

    def supplier_opening_fixture(self):
        from purchasing.models import Supplier
        supplier = Supplier.objects.create(shop=self.shop, name="Fournisseur")
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "balance_kind": "advance", "amount_fcfa": 30000,
                "occurred_at": "2026-01-01T10:00:00Z", "reason": "Solde antérieur"}
        return supplier, data, self.owner_bearer()

    def test_concurrent_supplier_opening_retries_keep_one_claim_without_cash(self):
        from purchasing.models import SupplierCredit
        supplier, data, bearer = self.supplier_opening_fixture()
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/suppliers/{supplier.id}/opening-balances/", data, bearer)
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(SupplierCredit.objects.get().remaining_fcfa, 30000)
        self.assertEqual(Activity.objects.filter(action="supplier_balance_opened").count(), 1)
        self.assertFalse(CashMovement.objects.exists())

    def test_concurrent_supplier_opening_allocations_cannot_consume_same_claim_twice(self):
        from purchasing.models import SupplierCredit, SupplierDebt
        from rest_framework.test import APIClient
        supplier, data, bearer = self.supplier_opening_fixture()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
        opened = client.post(f"/api/v1/shops/{self.shop.id}/suppliers/{supplier.id}/opening-balances/", data, format="json")
        self.assertEqual(opened.status_code, 201)
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="supplier_debt_opened", target_id=uuid.uuid4(), reason="Dette réelle")
        debt = SupplierDebt.objects.create(shop=self.shop, supplier=supplier, operation=event, installation=DeviceSession.objects.get(installation__user=self.owner).installation,
            occurred_at=timezone.now(), amount_fcfa=50000, remaining_fcfa=50000)
        allocation = {"operation_id": str(uuid.uuid4()), "reason": "Avance affectée", "allocations": [{"debt_id": str(debt.id), "amount_fcfa": 20000}]}
        second = {**allocation, "operation_id": str(uuid.uuid4())}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/suppliers/{supplier.id}/credits/{opened.data['id']}/allocate/", allocation,
                                    bearer, second_body=second)
        self.assertEqual(sorted(status for status, _ in results), [201, 400])
        self.assertEqual(SupplierCredit.objects.get().remaining_fcfa, 10000)
        debt.refresh_from_db()
        self.assertEqual(debt.remaining_fcfa, 30000)
        self.assertFalse(CashMovement.objects.exists())

    def test_concurrent_supplier_opening_retries_write_one_ledger_effect_and_statement(self):
        from purchasing.models import SupplierLedgerEntry
        supplier, data, bearer = self.supplier_opening_fixture()
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/suppliers/{supplier.id}/opening-balances/", data, bearer)
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        row = SupplierLedgerEntry.objects.get()
        self.assertEqual(row.delta_fcfa, -30000)
        self.assertEqual(row.advance_delta_fcfa, 30000)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
        statement = client.get(f"/api/v1/shops/{self.shop.id}/suppliers/{supplier.id}/statement/")
        self.assertEqual(statement.status_code, 200)
        self.assertEqual(statement.data["count"], 1)
        self.assertEqual(statement.data["closing_advance_fcfa"], 30000)
        self.assertEqual(statement.data["closing_balance_fcfa"], -30000)

    def test_concurrent_backdated_sale_retry_preserves_date_and_one_effect(self):
        product = Product.objects.create(shop=self.shop, name="Bouteille", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1000)
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "occurred_at": "2026-01-01T09:00:00Z", "reason": "Vente oubliée",
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000}]}
        results = self.simultaneous(f"/api/v1/shops/{self.shop.id}/sales/", data, self.owner_bearer())
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(StockMovement.objects.get().quantity_milli, -1000)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 1000)
        self.assertEqual(results[0][1]["occurred_at"], data["occurred_at"])
        self.assertEqual(Activity.objects.get(id=data["operation_id"]).reason, data["reason"])

    def test_concurrent_customer_return_and_physical_receipt_retries_have_one_effect_each(self):
        from sales.models import SaleReturn
        from stock.models import ReturnReceipt
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1000)
        bearer = self.owner_bearer()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
        sales_path = f"/api/v1/shops/{self.shop.id}/sales/"
        sale = client.post(sales_path, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
            "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000}]}, format="json")
        self.assertEqual(sale.status_code, 201)
        returns_path = sales_path + sale.data["id"] + "/returns/"
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
            "reason": "Retour client", "lines": [{"id": str(uuid.uuid4()), "sale_line_id": sale.data["lines"][0]["id"], "quantity_milli": 1000}]}
        results = self.simultaneous(returns_path, data, bearer)
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(SaleReturn.objects.count(), 1)
        receipt_path = returns_path + data["id"] + "/lines/" + data["lines"][0]["id"] + "/receipts/"
        physical = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
            "reason": "Article vendable reçu", "condition": "sellable", "quantity_milli": 1000}
        received = self.simultaneous(receipt_path, physical, bearer)
        self.assertEqual(sorted(status for status, _ in received), [200, 201])
        self.assertEqual(received[0][1], received[1][1])
        self.assertEqual(ReturnReceipt.objects.count(), 1)
        self.assertEqual(StockMovement.objects.get(kind="customer_return").quantity_milli, 1000)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 1000)

    def test_concurrent_distinct_returns_cannot_exceed_original_sale_quantity(self):
        from sales.models import SaleReturn
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1000)
        bearer = self.owner_bearer()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
        sales_path = f"/api/v1/shops/{self.shop.id}/sales/"
        sale = client.post(sales_path, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
            "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000}]}, format="json")
        self.assertEqual(sale.status_code, 201)
        def payload():
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
                "reason": "Retour", "lines": [{"id": str(uuid.uuid4()), "sale_line_id": sale.data["lines"][0]["id"], "quantity_milli": 1000}]}
        results = self.simultaneous(sales_path + sale.data["id"] + "/returns/", payload(), bearer, second_body=payload())
        self.assertEqual(sorted(status for status, _ in results), [201, 409])
        self.assertEqual(SaleReturn.objects.count(), 1)
        self.assertEqual(SaleReturn.objects.get().amount_fcfa, 1000)

    def return_credit_fixture(self, identified=False):
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1000)
        bearer = self.owner_bearer()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
        customer = Customer.objects.create(shop=self.shop, name="Client") if identified else None
        sales_path = f"/api/v1/shops/{self.shop.id}/sales/"
        def sale_payload(credit=False):
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "mode": "credit" if credit else "cash",
                "customer_id": str(customer.id) if customer else None,
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
                "payments": [] if credit else [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000}]}
        sale = client.post(sales_path, sale_payload(), format="json")
        self.assertEqual(sale.status_code, 201)
        path = sales_path + sale.data["id"] + "/returns/"
        returned = client.post(path, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "occurred_at": timezone.now().isoformat(), "reason": "Retour accepté",
            "lines": [{"id": str(uuid.uuid4()), "sale_line_id": sale.data["lines"][0]["id"], "quantity_milli": 1000}]}, format="json")
        self.assertEqual(returned.status_code, 201)
        debt = None
        if identified:
            new_sale = client.post(sales_path, sale_payload(True), format="json")
            self.assertEqual(new_sale.status_code, 201)
            debt = CustomerDebt.objects.get(sale_id=new_sale.data["id"])
        return path + returned.data["id"] + "/", returned.data["id"], bearer, debt

    def test_concurrent_return_refund_retry_has_one_negative_cash_effect(self):
        from customers.models import ReturnCreditSettlement
        path, returned_id, bearer, _ = self.return_credit_fixture()
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
            "reason": "Remboursement réel", "method": "cash", "amount_fcfa": 600}
        results = self.simultaneous(path + "refunds/", data, bearer)
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(ReturnCreditSettlement.objects.filter(sale_return_id=returned_id).count(), 1)
        self.assertEqual(CashMovement.objects.get(sale_return_refund_id=data["id"]).amount_fcfa, -600)
        self.assertEqual(CashMovement.objects.count(), 2)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_concurrent_refund_and_allocation_share_the_same_return_credit_pool(self):
        from customers.models import ReturnCreditSettlement
        path, returned_id, bearer, debt = self.return_credit_fixture(True)
        barrier = Barrier(2, timeout=10)
        def write(kind):
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
            data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(), "reason": "Décision explicite"}
            data.update({"method": "cash", "amount_fcfa": 600} if kind == "refunds" else
                {"allocations": [{"id": str(uuid.uuid4()), "debt_id": str(debt.id), "amount_fcfa": 600}]})
            try:
                barrier.wait()
                return client.post(path + kind + "/", data, format="json").status_code
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(write, kind) for kind in ("refunds", "allocations")]
            results = [future.result(timeout=30) for future in futures]
        self.assertEqual(sorted(results), [201, 409])
        decision = ReturnCreditSettlement.objects.get(sale_return_id=returned_id)
        self.assertEqual(decision.after_credit_fcfa, 400)
        self.assertEqual(decision.amount_fcfa, 600)
        debt.refresh_from_db()
        self.assertEqual(debt.remaining_fcfa, 400 if decision.kind == "allocation" else 1000)
        self.assertEqual(CashMovement.objects.count(), 2 if decision.kind == "refund" else 1)

    def unsellable_receipt_fixture(self):
        returned_path, _, bearer, _ = self.return_credit_fixture()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
        returned = client.get(returned_path)
        self.assertEqual(returned.status_code, 200)
        receipt_path = returned_path + "lines/" + returned.data["lines"][0]["id"] + "/receipts/"
        received = client.post(receipt_path, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "quantity_milli": 1000, "condition": "unsellable", "occurred_at": timezone.now().isoformat(), "reason": "Article non vendable reçu"}, format="json")
        self.assertEqual(received.status_code, 201)
        return receipt_path + received.data["id"] + "/decisions/", bearer

    def test_concurrent_unsellable_restock_retry_adds_available_stock_once(self):
        from stock.models import NonSellableDecision
        path, bearer = self.unsellable_receipt_fixture()
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "quantity_milli": 1000,
            "kind": "restock", "is_sellable_confirmed": True, "occurred_at": timezone.now().isoformat(), "reason": "État vendable confirmé"}
        results = self.simultaneous(path, data, bearer)
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(NonSellableDecision.objects.count(), 1)
        self.assertEqual(StockMovement.objects.get(kind="unsellable_restock").quantity_milli, 1000)
        self.assertEqual(CashMovement.objects.count(), 1)

    def test_concurrent_distinct_unsellable_outcomes_share_one_physical_pool(self):
        from stock.models import NonSellableDecision, StockLoss
        path, bearer = self.unsellable_receipt_fixture()
        def payload(kind):
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "quantity_milli": 1000,
                "kind": kind, "is_sellable_confirmed": kind == "restock", "occurred_at": timezone.now().isoformat(), "reason": "Décision explicite"}
        results = self.simultaneous(path, payload("scrap"), bearer, second_body=payload("restock"))
        self.assertEqual(sorted(status for status, _ in results), [201, 409])
        decision = NonSellableDecision.objects.get()
        self.assertEqual(decision.after_quantity_milli, 0)
        self.assertEqual(StockMovement.objects.filter(kind="unsellable_restock").count(), 1 if decision.kind == "restock" else 0)
        self.assertEqual(CashMovement.objects.count(), 1)
        self.assertFalse(StockLoss.objects.exists())

    def active_sale_cancellation_fixture(self):
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1000)
        bearer = self.owner_bearer()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
        path = f"/api/v1/shops/{self.shop.id}/sales/"
        sale = client.post(path, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 2000}],
            "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 2000}]}, format="json")
        self.assertEqual(sale.status_code, 201)
        return path + sale.data["id"] + "/", sale.data, bearer

    def test_concurrent_sale_cancellation_retry_creates_one_state_and_one_compensating_return(self):
        from sales.models import SaleCancellation, SaleReturn
        path, sale, bearer = self.active_sale_cancellation_fixture()
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(), "reason": "Annulation confirmée"}
        results = self.simultaneous(path + "cancellation/", data, bearer)
        self.assertEqual(sorted(status for status, _ in results), [200, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(SaleCancellation.objects.count(), 1)
        self.assertEqual(SaleReturn.objects.count(), 1)
        self.assertEqual(SaleReturn.objects.get().amount_fcfa, 2000)
        self.assertEqual(Activity.objects.filter(action="sale_cancelled").count(), 1)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 2000)
        self.assertEqual(StockMovement.objects.get().quantity_milli, -2000)

    def test_concurrent_partial_return_and_cancellation_never_reverse_more_than_original(self):
        from sales.models import SaleCancellation, SaleReturn, SaleReturnLine
        path, sale, bearer = self.active_sale_cancellation_fixture()
        at = timezone.now().isoformat()
        barrier = Barrier(2, timeout=10)
        def write(action):
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {bearer}")
            data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": at, "reason": "Décision simultanée"}
            if action == "returns":
                data["lines"] = [{"id": str(uuid.uuid4()), "sale_line_id": sale["lines"][0]["id"], "quantity_milli": 1000}]
            try:
                barrier.wait()
                return client.post(path + action + "/", data, format="json").status_code
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            cancellation = executor.submit(write, "cancellation")
            returned = executor.submit(write, "returns")
            cancellation_status, return_status = cancellation.result(timeout=30), returned.result(timeout=30)
        self.assertEqual(cancellation_status, 201)
        self.assertIn(return_status, [201, 409])
        self.assertEqual(SaleCancellation.objects.count(), 1)
        self.assertEqual(sum(SaleReturnLine.objects.values_list("quantity_milli", flat=True)), 2000)
        self.assertEqual(sum(SaleReturn.objects.values_list("amount_fcfa", flat=True)), 2000)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 2000)
        self.assertEqual(StockMovement.objects.get().quantity_milli, -2000)
