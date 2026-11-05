"""US-14/15: non-sellable outcomes share one physical quantity and keep cash distinct."""
import uuid
from django.test import TestCase
from django.utils import timezone
from stock.tests import test_unsellable_decision_models as model_tests
from cash.models import CashMovement
from stock.models import NonSellableDecision, ReturnReceipt, StockAlertEvent, StockAlertState, StockLoss, StockMovement
from catalogue.models import Product
from sales.models import SaleLine
from purchasing.models import Supplier, SupplierCredit, SupplierDebt, SupplierReturn


class NonSellableDecisionTests(TestCase):
    setUp = model_tests.NonSellableDecisionModelTests.setUp
    authenticate = model_tests.NonSellableDecisionModelTests.authenticate
    payload = model_tests.NonSellableDecisionModelTests.payload
    url = model_tests.NonSellableDecisionModelTests.url
    prepare_sale = model_tests.NonSellableDecisionModelTests.prepare_sale
    return_url = model_tests.NonSellableDecisionModelTests.return_url
    return_payload = model_tests.NonSellableDecisionModelTests.return_payload
    prepare_return = model_tests.NonSellableDecisionModelTests.prepare_return
    receipts_url = model_tests.NonSellableDecisionModelTests.receipts_url
    receipt_payload = model_tests.NonSellableDecisionModelTests.receipt_payload
    prepare_unsellable = model_tests.NonSellableDecisionModelTests.prepare_unsellable

    def decisions_url(self, receipt):
        line = receipt.return_line
        return f"/api/v1/shops/{self.shop.id}/sales/{line.sale_line.sale_id}/returns/{line.sale_return_id}/lines/{line.id}/receipts/{receipt.id}/decisions/"

    def decision_payload(self, kind="scrap", quantity=1000, **extra):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "kind": kind, "quantity_milli": quantity,
                "occurred_at": timezone.now().isoformat(), "reason": "Décision physique confirmée", **extra}

    def test_restock_then_scrap_consumes_non_sellable_pool_without_duplicate_loss_or_cash(self):
        StockMovement.objects.filter(kind="opening").update(unit_cost_micros=None)
        Product.objects.filter(id=self.product.id).update(stock_threshold_milli=8000)
        receipt = self.prepare_unsellable()
        path = self.decisions_url(receipt)
        self.assertTrue(StockAlertState.objects.get(product=self.product).is_low)
        restock = self.decision_payload("restock", is_sellable_confirmed=True)
        response = self.client.post(path, restock, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["after_quantity_milli"], 1000)
        self.assertEqual(response.data["receipt_id"], str(receipt.id))
        self.assertIsNotNone(response.data["movement_id"])
        self.assertIsNone(response.data["unit_cost_micros"])
        self.assertTrue(response.data["cost_is_provisional"])
        self.assertEqual(self.client.post(path, restock, format="json").data, response.data)
        self.assertFalse(StockAlertState.objects.get(product=self.product).is_low)
        scrap = self.decision_payload()
        discarded = self.client.post(path, scrap, format="json")
        self.assertEqual(discarded.status_code, 201, discarded.content)
        self.assertEqual(discarded.data["after_quantity_milli"], 0)
        self.assertIsNone(discarded.data["movement_id"])
        self.assertEqual(self.client.post(path, self.decision_payload(), format="json").status_code, 409)
        self.assertEqual(StockMovement.objects.get(kind="unsellable_restock").quantity_milli, 1000)
        self.assertEqual(StockMovement.objects.count(), 3)
        self.assertEqual(CashMovement.objects.count(), 2)
        self.assertFalse(StockLoss.objects.exists())
        self.assertEqual(self.client.get(path).data["count"], 2)
        receipt_url = path.removesuffix("decisions/")
        self.assertEqual(self.client.get(receipt_url).data["remaining_unsellable_milli"], 0)
        next_sale = self.payload(lines=[{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1, "quantity_milli": 1000}],
            payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 5000}])
        self.assertEqual(self.client.post(self.url(), next_sale, format="json").status_code, 201)
        self.assertEqual(StockAlertEvent.objects.filter(product=self.product).count(), 2)

    def test_supplier_departure_does_not_remove_available_stock_or_create_debt_credit_or_cash(self):
        receipt = self.prepare_unsellable()
        supplier = Supplier.objects.create(shop=self.shop, name="Fournisseur")
        data = self.decision_payload("supplier_return", supplier_id=str(supplier.id))
        path = self.decisions_url(receipt)
        departed = self.client.post(path, data, format="json")
        self.assertEqual(departed.status_code, 201, departed.content)
        self.assertEqual(departed.data["supplier_id"], str(supplier.id))
        self.assertEqual(self.client.post(path, data, format="json").data, departed.data)
        self.assertEqual(StockMovement.objects.count(), 2)
        self.assertEqual(CashMovement.objects.count(), 2)
        self.assertFalse(SupplierDebt.objects.exists())
        self.assertFalse(SupplierCredit.objects.exists())
        self.assertFalse(SupplierReturn.objects.exists())
        journal = self.client.get(f"/api/v1/shops/{self.shop.id}/suppliers/{supplier.id}/statement/")
        self.assertEqual(journal.status_code, 200)
        self.assertEqual(journal.data["count"], 1)
        self.assertEqual(journal.data["results"][0]["kind"], "unsellable_return")
        self.assertEqual(journal.data["closing_balance_fcfa"], 0)

    def test_restock_needs_explicit_sellable_confirmation_and_keeps_original_receipt_cost(self):
        StockMovement.objects.filter(kind="opening").update(unit_cost_micros=1200000000)
        receipt = self.prepare_unsellable()
        SaleLine.objects.update(unit_cost_micros=9900000000)
        path = self.decisions_url(receipt)
        self.assertEqual(self.client.post(path, self.decision_payload("restock"), format="json").status_code, 400)
        response = self.client.post(path, self.decision_payload("restock", is_sellable_confirmed=True), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["unit_cost_micros"], 1200000000)
        self.assertFalse(response.data["cost_is_provisional"])
        self.assertEqual(StockMovement.objects.get(kind="unsellable_restock").unit_cost_micros, 1200000000)

    def test_seller_foreign_supplier_invalid_quantity_date_and_changed_retry_have_no_extra_effect(self):
        receipt = self.prepare_unsellable()
        path = self.decisions_url(receipt)
        data = self.decision_payload()
        self.authenticate(self.seller)
        self.assertEqual(self.client.post(path, data, format="json").status_code, 403)
        self.assertEqual(self.client.get(path).status_code, 403)
        self.authenticate(self.owner)
        self.assertEqual(self.client.post(path.replace(str(self.shop.id), str(self.other.id)), data, format="json").status_code, 404)
        foreign = Supplier.objects.create(shop=self.other, name="Autre boutique")
        self.assertEqual(self.client.post(path, self.decision_payload("supplier_return", supplier_id=str(foreign.id)), format="json").status_code, 404)
        for changes in ({"quantity_milli": 1500}, {"quantity_milli": 1.5}, {"reason": " "},
                        {"occurred_at": "2026-01-01T00:00:00Z"}, {"kind": "supplier_return"},
                        {"supplier_id": str(foreign.id)}, {"is_sellable_confirmed": True}):
            with self.subTest(changes=changes):
                self.assertEqual(self.client.post(path, {**data, **changes}, format="json").status_code, 400)
        self.assertFalse(NonSellableDecision.objects.exists())
        self.assertEqual(self.client.post(path, data, format="json").status_code, 201)
        self.assertEqual(self.client.post(path, {**data, "kind": "restock", "is_sellable_confirmed": True}, format="json").status_code, 409)
        self.assertFalse(StockMovement.objects.filter(kind="unsellable_restock").exists())

    def test_shop_pool_lists_only_authorized_remaining_receipts_and_can_include_closed_history(self):
        StockMovement.objects.filter(kind="opening").update(unit_cost_micros=None)
        receipt = self.prepare_unsellable()
        pool = f"/api/v1/shops/{self.shop.id}/unsellable-stock/"
        listed = self.client.get(pool)
        self.assertEqual(listed.status_code, 200, listed.content)
        self.assertEqual(listed.data["count"], 1)
        row = listed.data["results"][0]
        self.assertEqual(row["id"], str(receipt.id))
        self.assertEqual(row["product_id"], str(self.product.id))
        self.assertEqual(row["remaining_unsellable_milli"], 2000)
        self.assertIsNone(row["unit_cost_micros"])
        self.assertEqual(self.client.get(pool, {"product_id": str(self.foreign.id)}).data["count"], 0)
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.other.id}/unsellable-stock/").data["count"], 0)
        self.assertEqual(self.client.post(self.decisions_url(receipt), self.decision_payload(quantity=2000), format="json").status_code, 201)
        self.assertEqual(self.client.get(pool).data["count"], 0)
        closed = self.client.get(pool, {"include_closed": "true"})
        self.assertEqual(closed.data["count"], 1)
        self.assertEqual(closed.data["results"][0]["remaining_unsellable_milli"], 0)
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(pool).status_code, 403)

    def test_exhausted_pool_conflict_is_localized_in_english(self):
        receipt = self.prepare_unsellable()
        path = self.decisions_url(receipt)
        self.assertEqual(self.client.post(path, self.decision_payload(quantity=2000), format="json").status_code, 201)
        denied = self.client.post(path, self.decision_payload(), format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.json()["code"], "unsellable_quantity_changed")
        self.assertEqual(denied.json()["detail"], "Remaining unsellable quantity has changed. Read it again before confirming.")

    def test_weight_decisions_keep_thousandths_and_sellable_receipts_cannot_be_disposed_again(self):
        sale = self.prepare_sale(lines=[{"id": str(uuid.uuid4()), "product_id": str(self.weight.id), "product_version": 1, "quantity_milli": 500}],
            payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 125}])
        returned = self.client.post(self.return_url(sale), self.return_payload(sale, 500), format="json")
        self.assertEqual(returned.status_code, 201)
        received = self.client.post(self.receipts_url(sale, returned.data), self.receipt_payload(quantity=500), format="json")
        self.assertEqual(received.status_code, 201)
        receipt = ReturnReceipt.objects.get(id=received.data["id"])
        path = self.decisions_url(receipt)
        first = self.client.post(path, self.decision_payload("restock", quantity=125, is_sellable_confirmed=True), format="json")
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(first.data["after_quantity_milli"], 375)
        self.assertEqual(self.client.post(path, self.decision_payload(quantity=375), format="json").status_code, 201)
        self.assertEqual(StockMovement.objects.get(kind="unsellable_restock").quantity_milli, 125)
        self.assertEqual(self.client.get(path.removesuffix("decisions/")).data["remaining_unsellable_milli"], 0)
        other_sale, other_return = self.prepare_return()
        sellable = self.client.post(self.receipts_url(other_sale, other_return), self.receipt_payload("sellable"), format="json")
        self.assertEqual(sellable.status_code, 201)
        other_receipt = ReturnReceipt.objects.get(id=sellable.data["id"])
        self.assertEqual(self.client.post(self.decisions_url(other_receipt), self.decision_payload(), format="json").status_code, 404)

    def test_shop_pool_is_paged_in_sql_after_filtering_and_rejects_invalid_filters(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        for _index in range(3):
            self.prepare_unsellable()
        pool = f"/api/v1/shops/{self.shop.id}/unsellable-stock/"
        with CaptureQueriesContext(connection) as queries:
            first = self.client.get(pool, {"page_size": 2, "product_id": str(self.product.id)})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["count"], 3)
        self.assertEqual(len(first.data["results"]), 2)
        self.assertTrue(any("LIMIT 2" in row["sql"].upper() for row in queries.captured_queries))
        second = self.client.get(first.data["next"])
        self.assertEqual(len(second.data["results"]), 1)
        self.assertNotIn(second.data["results"][0]["id"], {row["id"] for row in first.data["results"]})
        for query in ({"product_id": "invalid"}, {"include_closed": "invalid"}, {"unexpected": "value"}):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(pool, query).status_code, 400)
