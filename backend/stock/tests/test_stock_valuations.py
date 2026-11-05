"""US-33: a manager explicitly values remaining stock, preserving physical and money facts."""
import uuid
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from common.models import Activity
from cash.models import CashMovement
from identity.models import DeviceSession, Installation, Membership, Shop
from catalogue.models import Product
from stock.models import StockCostSnapshot, StockMovement, StockValuationAdjustment
from purchasing.models import SupplierCredit
from stock.tests import test_return_costs as fixtures


class StockValuationTests(TestCase):
    setUp = fixtures.ReturnCostTests.setUp
    authenticate = fixtures.ReturnCostTests.authenticate
    url = fixtures.ReturnCostTests.url
    payload = fixtures.ReturnCostTests.payload
    invoice_and_receipts = fixtures.ReturnCostTests.invoice_and_receipts
    make_sale = fixtures.ReturnCostTests.make_sale
    event = fixtures.ReturnCostTests.event

    def path(self):
        return f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/stock-valuations/"

    def gap(self):
        Product.objects.filter(id=self.product.id).update(unit_kind="weight", unit_label="kg")
        invoice, _ = self.invoice_and_receipts(total_price=10000, quantity=1000, parts=(1000,))
        self.invoice_and_receipts(total_price=1, quantity=100000, parts=(100000,))
        sale = self.make_sale(100000)
        departure = self.client.post(self.url() + invoice["id"] + "/returns/", self.event(
            lines=[{"line_id": invoice["lines"][0]["id"], "quantity_milli": 500}]), format="json")
        self.assertEqual(departure.status_code, 201, departure.content)
        return sale, departure

    def preview(self, value=600):
        result = self.client.post(self.path() + "preview/", {"value_fcfa": value}, format="json")
        self.assertEqual(result.status_code, 200, result.content)
        return result

    def body(self, preview, value=600):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "value_fcfa": value,
            "preview_token": preview.data["preview_token"], "reason": "Valeur restante contrôlée"}

    def test_model_requires_positive_exact_quantity_nonnegative_money_and_preserves_unknown_before(self):
        operation = Activity.objects.create(shop=self.shop, actor=self.owner, action="stock_valuation_confirmed",
            target_id=self.product.id, reason="Coût restant contrôlé")
        row = StockValuationAdjustment.objects.create(product=self.product, operation=operation,
            installation=Installation.objects.get(user=self.owner), occurred_at=timezone.now(),
            stock_quantity_milli=500, previous_value_nanos_fcfa=None, value_fcfa=600, basis_fingerprint="a" * 64)
        row.refresh_from_db()
        self.assertIsNone(row.previous_value_nanos_fcfa)
        self.assertEqual(row.stock_quantity_milli, 500)
        for changes in ({"stock_quantity_milli": 0}, {"stock_quantity_milli": True},
                {"stock_quantity_milli": "500"}, {"previous_value_nanos_fcfa": -1}, {"value_fcfa": -1},
                {"value_fcfa": 0.5}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                StockValuationAdjustment.objects.filter(id=row.id).update(**changes)

    def test_preview_is_read_only_confirmation_changes_remaining_cost_and_retry_has_no_second_effect(self):
        sale, departure = self.gap()
        snapshots = list(StockCostSnapshot.objects.order_by("id").values())
        count = Activity.objects.count()
        pending = self.client.get(self.path() + "current/")
        self.assertEqual(pending.data["stock_value_state"], "unknown")
        self.assertTrue(pending.data["has_unresolved_valuation"])
        self.assertFalse(pending.data["has_unresolved_shortage"])
        preview = self.preview()
        self.assertEqual(preview.data["quantity_milli"], "500")
        self.assertIsNone(preview.data["previous_value_nanos_fcfa"])
        self.assertEqual(preview.data["value_nanos_fcfa"], "600000000000")
        self.assertEqual(Activity.objects.count(), count)
        body = self.body(preview)
        result = self.client.post(self.path(), body, format="json")
        self.assertEqual(result.status_code, 201, result.content)
        self.assertEqual(self.client.post(self.path(), body, format="json").data, result.data)
        self.assertEqual(StockValuationAdjustment.objects.count(), 1)
        self.assertEqual(list(StockCostSnapshot.objects.order_by("id").values()), snapshots)
        self.assertEqual(sum(StockMovement.objects.values_list("quantity_milli", flat=True)), 500)
        self.assertEqual(CashMovement.objects.count(), 1)
        self.assertFalse(SupplierCredit.objects.exists())
        current = self.client.get(self.path() + "current/")
        self.assertEqual(current.status_code, 200, current.content)
        self.assertEqual(current.data["stock_value_nanos_fcfa"], "600000000000")
        self.assertFalse(current.data["valuation_requires_regularization"])
        self.assertEqual(current.data["stock_value_state"], "known")
        self.assertFalse(current.data["has_unresolved_valuation"])
        next_sale = self.make_sale(500)
        cost = StockCostSnapshot.objects.get(movement__sale_line__sale_id=next_sale["id"])
        self.assertEqual(cost.total_cost_nanos_fcfa, 600000000000)
        original = StockCostSnapshot.objects.get(movement__sale_line__sale_id=sale["id"])
        self.assertEqual(original.total_cost_nanos_fcfa, 10000000000000)
        self.assertEqual(departure.data["lines"][0]["cost"]["valuation_gap_nanos_fcfa"], "-4900000000000")

    def test_changed_preview_or_amount_is_rejected_before_any_decision(self):
        self.invoice_and_receipts(total_price=1000, quantity=2000, parts=(2000,))
        preview = self.preview()
        body = self.body(preview)
        changed_amount = self.client.post(self.path(), {**body, "value_fcfa": 601}, format="json")
        self.assertEqual(changed_amount.status_code, 409, changed_amount.content)
        self.assertEqual(changed_amount.data["code"], "stock_valuation_preview_changed")
        self.make_sale(1000)
        stale = self.client.post(self.path(), body, format="json")
        self.assertEqual(stale.status_code, 409, stale.content)
        self.assertFalse(StockValuationAdjustment.objects.exists())

    def test_late_prior_receipt_invalidates_confirmation_without_inventing_a_new_value(self):
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        preview = self.preview()
        body = self.body(preview)
        result = self.client.post(self.path(), body, format="json")
        self.assertEqual(result.status_code, 201, result.content)
        self.invoice_and_receipts(total_price=1200, quantity=1000, parts=(1000,))
        current = self.client.get(self.path() + "current/")
        self.assertEqual(current.status_code, 200, current.content)
        self.assertEqual(current.data["quantity_milli"], "2000")
        self.assertIsNone(current.data["stock_value_nanos_fcfa"])
        self.assertTrue(current.data["valuation_requires_regularization"])
        self.assertEqual(current.data["invalid_adjustments_count"], 1)
        self.assertEqual(self.client.post(self.path(), body, format="json").data, result.data)

    def test_stock_sold_out_after_preview_is_a_conflict_not_a_new_valuation(self):
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        preview = self.preview()
        self.make_sale(1000)
        response = self.client.post(self.path(), self.body(preview), format="json")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.data["code"], "stock_valuation_preview_changed")
        self.assertFalse(StockValuationAdjustment.objects.exists())

    def test_late_fee_invalidates_basis_even_when_quantity_did_not_change(self):
        invoice, _ = self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        preview = self.preview()
        result = self.client.post(self.path(), self.body(preview), format="json")
        self.assertEqual(result.status_code, 201, result.content)
        fee = self.client.post(self.url() + invoice["id"] + "/charges/", self.event(label="Frais tardifs",
            amount_fcfa=100, allocations=[{"line_id": invoice["lines"][0]["id"], "amount_fcfa": 100}]), format="json")
        self.assertEqual(fee.status_code, 201, fee.content)
        current = self.client.get(self.path() + "current/")
        self.assertTrue(current.data["valuation_requires_regularization"])
        self.assertIsNone(current.data["stock_value_nanos_fcfa"])
        self.assertEqual(StockValuationAdjustment.objects.get().value_fcfa, 600)

    def test_cost_replay_preserves_confirmed_value_and_detects_a_new_valuation_in_its_preview(self):
        self.gap()
        revaluations = f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/cost-revaluations/"
        old_preview = self.client.get(revaluations + "preview/")
        self.assertEqual(old_preview.status_code, 200, old_preview.content)
        preview = self.preview()
        recorded = self.client.post(self.path(), self.body(preview), format="json")
        self.assertEqual(recorded.status_code, 201, recorded.content)
        stale = self.client.post(revaluations, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "preview_token": old_preview.data["preview_token"], "reason": "Aperçu devenu ancien"}, format="json")
        self.assertEqual(stale.status_code, 409, stale.content)
        fresh = self.client.get(revaluations + "preview/")
        self.assertEqual(fresh.data["stock_value_after_nanos_fcfa"], "600000000000")
        result = self.client.post(revaluations, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "preview_token": fresh.data["preview_token"], "reason": "Historique contrôlé"}, format="json")
        self.assertEqual(result.status_code, 201, result.content)
        self.assertEqual(self.client.get(self.path() + "current/").data["stock_value_nanos_fcfa"], "600000000000")
        self.assertEqual(StockValuationAdjustment.objects.count(), 1)

    def test_current_rights_scoping_input_and_paged_history(self):
        self.invoice_and_receipts(total_price=0, quantity=1000, parts=(1000,))
        for value in (True, -1, 0.5, "600"):
            with self.subTest(value=value):
                self.assertEqual(self.client.post(self.path() + "preview/", {"value_fcfa": value}, format="json").status_code, 400)
        preview = self.preview(0)
        result = self.client.post(self.path(), self.body(preview, 0), format="json")
        self.assertEqual(result.status_code, 201, result.content)
        page = self.client.get(self.path(), {"page_size": 1})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.data["count"], 1)
        self.assertEqual(page.data["results"][0], result.data)
        detail = self.client.get(self.path() + result.data["id"] + "/")
        self.assertEqual(detail.data, result.data)
        other = Shop.objects.create(owner=self.owner, name="Autre boutique")
        foreign = self.path().replace(str(self.shop.id), str(other.id))
        self.assertEqual(self.client.get(foreign).status_code, 404)
        self.assertEqual(self.client.get(foreign + result.data["id"] + "/").status_code, 404)
        self.assertEqual(self.client.post(self.path(), {**self.body(self.preview(0), 0),
            "id": result.data["id"]}, format="json").status_code, 409)
        self.authenticate(self.seller)
        Membership.objects.filter(user=self.seller).update(can_read_cost=True)
        for suffix in ("", "current/"):
            self.assertEqual(self.client.get(self.path() + suffix).status_code, 403)
        self.assertEqual(self.client.post(self.path() + "preview/", {"value_fcfa": 600}, format="json").status_code, 403)
        self.authenticate(self.owner)
        DeviceSession.objects.filter(installation__user=self.owner).update(revoked_at=timezone.now())
        self.assertEqual(self.client.get(self.path() + "current/").status_code, 401)

    def test_no_positive_stock_refuses_remaining_value_decision(self):
        response = self.client.post(self.path() + "preview/", {"value_fcfa": 600}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_large_confirmed_value_remains_exact_and_old_shortage_stays_pending(self):
        from sales.services.pricing import MAX_FCFA
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        old_sale = self.make_sale(2000)
        _, incoming = self.invoice_and_receipts(total_price=1200, quantity=3000, parts=(3000,))
        StockMovement.objects.filter(id=incoming[0].id).update(occurred_at=timezone.now())
        preview = self.preview(MAX_FCFA)
        self.assertTrue(preview.data["valuation_requires_regularization_after"])
        result = self.client.post(self.path(), self.body(preview, MAX_FCFA), format="json")
        self.assertEqual(result.status_code, 201, result.content)
        self.assertEqual(result.data["value_fcfa"], "9223372036854775807")
        current = self.client.get(self.path() + "current/")
        self.assertEqual(current.data["stock_value_nanos_fcfa"], "9223372036854775807000000000")
        self.assertTrue(current.data["valuation_requires_regularization"])
        self.assertTrue(current.data["has_unresolved_shortage"])
        self.assertEqual(current.data["stock_value_state"], "known")
        old_cost = StockCostSnapshot.objects.get(movement__sale_line__sale_id=old_sale["id"])
        self.assertEqual(old_cost.cost_state, "provisional")
        self.assertEqual(old_cost.total_cost_nanos_fcfa, 2000000000000)

    def test_late_prior_receipt_does_not_clear_a_captured_sale_shortage(self):
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        sale = self.make_sale(2000)
        self.invoice_and_receipts(total_price=1200, quantity=2000, parts=(2000,))
        current = self.client.get(self.path() + "current/")
        self.assertEqual(current.data["quantity_milli"], "1000")
        self.assertTrue(current.data["has_unresolved_shortage"])
        self.assertTrue(current.data["valuation_requires_regularization"])
        cost = StockCostSnapshot.objects.get(movement__sale_line__sale_id=sale["id"])
        self.assertEqual(cost.cost_state, "provisional")
        self.assertTrue(self.preview().data["valuation_requires_regularization_after"])

    def test_late_prior_receipt_does_not_clear_a_captured_loss_shortage(self):
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        loss = self.client.post(f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/losses/",
            self.event(quantity_milli=2000), format="json")
        self.assertEqual(loss.status_code, 201, loss.content)
        self.invoice_and_receipts(total_price=1200, quantity=2000, parts=(2000,))
        current = self.client.get(self.path() + "current/")
        self.assertEqual(current.data["quantity_milli"], "1000")
        self.assertTrue(current.data["has_unresolved_shortage"])
        self.assertEqual(StockCostSnapshot.objects.get(movement__kind="loss").cost_state, "provisional")
