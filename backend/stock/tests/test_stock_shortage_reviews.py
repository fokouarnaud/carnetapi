"""US-13/33: a manager reviews historical physical shortages independently of costs."""
import uuid
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from common.models import Activity
from cash.models import CashMovement
from identity.models import DeviceSession, Installation, Membership, Shop
from catalogue.models import Product
from stock.models import StockCostSnapshot, StockMovement, StockShortageReview
from purchasing.models import SupplierDebt
from stock.tests import test_stock_valuations as fixtures


class StockShortageReviewTests(TestCase):
    setUp = fixtures.StockValuationTests.setUp
    authenticate = fixtures.StockValuationTests.authenticate
    url = fixtures.StockValuationTests.url
    payload = fixtures.StockValuationTests.payload
    invoice_and_receipts = fixtures.StockValuationTests.invoice_and_receipts
    make_sale = fixtures.StockValuationTests.make_sale
    event = fixtures.StockValuationTests.event

    def path(self):
        return f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/stock-shortage-reviews/"

    def current(self):
        return self.client.get(f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/stock-valuations/current/")

    def shortage(self):
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        sale = self.make_sale(2000)
        invoice, incoming = self.invoice_and_receipts(total_price=1200, quantity=3000, parts=(3000,))
        StockMovement.objects.filter(id=incoming[0].id).update(occurred_at=timezone.now())
        return sale, invoice

    def preview(self, **data):
        response = self.client.post(self.path() + "preview/", data, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def body(self, preview, **extra):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "reason": "Manque historique examiné",
            "preview_token": preview.data["preview_token"], **extra}

    def test_model_preserves_current_quantity_and_unknown_value_with_exact_guards(self):
        operation = Activity.objects.create(shop=self.shop, actor=self.owner, action="stock_shortage_reviewed",
            target_id=self.product.id, reason="Contrôle physique")
        review = StockShortageReview.objects.create(product=self.product, operation=operation,
            installation=Installation.objects.get(user=self.owner), occurred_at=timezone.now(),
            stock_quantity_milli=1000, stock_value_nanos_fcfa=None, basis_fingerprint="a" * 64)
        self.assertIsNone(StockShortageReview.objects.get(id=review.id).stock_value_nanos_fcfa)
        for changes in ({"stock_quantity_milli": -1}, {"stock_quantity_milli": True}, {"stock_quantity_milli": "1000"},
                {"stock_value_nanos_fcfa": -1}, {"stock_quantity_milli": 0, "stock_value_nanos_fcfa": None}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                StockShortageReview.objects.filter(id=review.id).update(**changes)

    def test_preview_and_explicit_review_clear_only_shortage_and_retry_is_stable(self):
        sale, _ = self.shortage()
        before = list(StockCostSnapshot.objects.order_by("id").values())
        activity_count = Activity.objects.count()
        preview = self.preview()
        self.assertEqual(preview.data["quantity_milli"], "2000")
        self.assertEqual(preview.data["stock_value_nanos_fcfa"], "2400000000000")
        self.assertEqual(preview.data["movement_count"], 3)
        page = self.client.get(self.path() + "movements/", {"page_size": 1})
        self.assertEqual(page.status_code, 200, page.content)
        self.assertEqual(page.data["count"], 3)
        self.assertEqual(len(page.data["results"]), 1)
        self.assertIsNotNone(page.data["next"])
        sold = self.client.get(page.data["next"]).data["results"][0]
        self.assertEqual(sold["source_id"], sale["id"])
        self.assertEqual(sold["quantity_milli"], "-2000")
        self.assertEqual(sold["cost"]["cost_state"], "provisional")
        self.assertEqual(Activity.objects.count(), activity_count)
        body = self.body(preview)
        reviewed = self.client.post(self.path(), body, format="json")
        self.assertEqual(reviewed.status_code, 201, reviewed.content)
        self.assertIsNone(reviewed.data["count_id"])
        self.assertEqual(self.client.post(self.path(), body, format="json").data, reviewed.data)
        self.assertEqual(self.client.post(self.path(), {**body, "reason": "Autre motif"}, format="json").status_code, 409)
        current = self.current()
        self.assertFalse(current.data["has_unresolved_shortage"])
        self.assertEqual(current.data["stock_value_nanos_fcfa"], "2400000000000")
        self.assertEqual(StockShortageReview.objects.count(), 1)
        self.assertEqual(StockMovement.objects.count(), 3)
        self.assertEqual(list(StockCostSnapshot.objects.order_by("id").values()), before)
        self.assertEqual(StockCostSnapshot.objects.get(movement__sale_line__sale_id=sale["id"]).cost_state, "provisional")
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 200)
        self.assertEqual(sum(SupplierDebt.objects.values_list("remaining_fcfa", flat=True)), 4600)
        self.assertEqual(self.client.get(self.path()).data["results"][0], reviewed.data)
        self.assertEqual(self.client.get(self.path() + reviewed.data["id"] + "/").data, reviewed.data)

    def test_fee_changes_do_not_reopen_physical_review_but_prior_physical_receipt_does(self):
        _, invoice = self.shortage()
        preview = self.preview()
        body = self.body(preview)
        result = self.client.post(self.path(), body, format="json")
        self.assertEqual(result.status_code, 201, result.content)
        fee = self.client.post(self.url() + invoice["id"] + "/charges/", self.event(label="Frais tardifs", amount_fcfa=300,
            allocations=[{"line_id": invoice["lines"][0]["id"], "amount_fcfa": 300}]), format="json")
        self.assertEqual(fee.status_code, 201, fee.content)
        self.assertFalse(self.current().data["has_unresolved_shortage"])
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        current = self.current()
        self.assertTrue(current.data["has_unresolved_shortage"])
        self.assertEqual(current.data["invalid_shortage_reviews_count"], 1)
        self.assertEqual(self.client.post(self.path(), body, format="json").data, result.data)
        self.assertTrue(self.current().data["has_unresolved_shortage"])

    def test_new_shortage_after_review_is_not_covered_by_the_old_decision(self):
        self.shortage()
        result = self.client.post(self.path(), self.body(self.preview()), format="json")
        self.assertEqual(result.status_code, 201, result.content)
        self.make_sale(3000)
        self.assertTrue(self.current().data["has_unresolved_shortage"])
        self.assertEqual(self.client.post(self.path() + "preview/", {}, format="json").status_code, 400)
        self.assertEqual(StockShortageReview.objects.count(), 1)

    def test_stale_preview_and_blank_reason_have_no_review_effect(self):
        self.shortage()
        preview = self.preview()
        body = self.body(preview)
        self.assertEqual(self.client.post(self.path(), {**body, "reason": ""}, format="json").status_code, 400)
        self.make_sale(1000)
        stale = self.client.post(self.path(), body, format="json")
        self.assertEqual(stale.status_code, 409, stale.content)
        self.assertFalse(StockShortageReview.objects.exists())

    def test_optional_count_is_scoped_and_bound_to_preview(self):
        self.shortage()
        other_product = Product.objects.create(shop=self.shop, name="Autre produit", unit_kind="piece",
            unit_label="pièce", sale_price_fcfa=100)
        unrelated = self.client.post(f"/api/v1/shops/{self.shop.id}/products/{other_product.id}/stock-counts/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "physical_milli": 0,
            "counted_at": timezone.now().isoformat(), "reason": "Autre produit compté"}, format="json")
        self.assertEqual(unrelated.status_code, 201, unrelated.content)
        self.assertEqual(self.client.post(self.path() + "preview/", {"count_id": unrelated.data["id"]}, format="json").status_code, 404)
        counts = f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/stock-counts/"
        count = self.client.post(counts, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "physical_milli": 2000, "counted_at": timezone.now().isoformat(), "reason": "Contrôle physique"}, format="json")
        self.assertEqual(count.status_code, 201, count.content)
        preview = self.preview(count_id=count.data["id"])
        body = self.body(preview, count_id=count.data["id"])
        changed = self.client.post(self.path(), {**body, "count_id": None}, format="json")
        self.assertEqual(changed.status_code, 409, changed.content)
        result = self.client.post(self.path(), body, format="json")
        self.assertEqual(result.status_code, 201, result.content)
        self.assertEqual(result.data["count_id"], count.data["id"])
        self.assertEqual(StockMovement.objects.count(), 3)

    def test_review_permissions_are_current_and_shop_scoped(self):
        self.shortage()
        self.authenticate(self.seller)
        Membership.objects.filter(user=self.seller).update(can_read_cost=True)
        self.assertEqual(self.client.get(self.path()).status_code, 403)
        self.assertEqual(self.client.get(self.path() + "movements/").status_code, 403)
        self.assertEqual(self.client.post(self.path() + "preview/", {}, format="json").status_code, 403)
        self.authenticate(self.owner)
        other = Shop.objects.create(owner=self.owner, name="Autre boutique")
        self.assertEqual(self.client.get(self.path().replace(str(self.shop.id), str(other.id))).status_code, 404)
        DeviceSession.objects.filter(installation__user=self.owner).update(revoked_at=timezone.now())
        self.assertEqual(self.client.post(self.path() + "preview/", {}, format="json").status_code, 401)

    def test_no_historical_shortage_cannot_create_a_review(self):
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        self.assertEqual(self.client.post(self.path() + "preview/", {}, format="json").status_code, 400)

    def test_physical_review_does_not_clear_an_unresolved_value_confirmation(self):
        _, invoice = self.shortage()
        values = f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/stock-valuations/"
        preview = self.client.post(values + "preview/", {"value_fcfa": 2400}, format="json")
        self.assertEqual(preview.status_code, 200, preview.content)
        body = {**self.body(preview), "value_fcfa": 2400}
        self.assertEqual(self.client.post(values, body, format="json").status_code, 201)
        fee = self.client.post(self.url() + invoice["id"] + "/charges/", self.event(label="Frais tardifs", amount_fcfa=300,
            allocations=[{"line_id": invoice["lines"][0]["id"], "amount_fcfa": 300}]), format="json")
        self.assertEqual(fee.status_code, 201, fee.content)
        preview = self.preview()
        self.assertTrue(preview.data["valuation_requires_regularization_after"])
        result = self.client.post(self.path(), self.body(preview), format="json")
        self.assertEqual(result.status_code, 201, result.content)
        current = self.current()
        self.assertFalse(current.data["has_unresolved_shortage"])
        self.assertTrue(current.data["has_unresolved_valuation"])
        self.assertIsNone(current.data["stock_value_nanos_fcfa"])

    def test_negative_stock_requires_explicit_count_correction_then_independent_value_confirmation(self):
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        old_sale = self.make_sale(2000)
        self.assertEqual(self.client.post(self.path() + "preview/", {}, format="json").status_code, 400)
        counts = f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/stock-counts/"
        count = self.client.post(counts, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "physical_milli": 1000, "counted_at": timezone.now().isoformat(), "reason": "Stock physique vérifié"}, format="json")
        self.assertEqual(count.status_code, 201, count.content)
        correction = self.client.post(counts + count.data["id"] + "/confirm/", {"operation_id": str(uuid.uuid4()),
            "expected_milli": -1000, "applied_adjustment_milli": 0, "reason": "Correction physique confirmée"}, format="json")
        self.assertEqual(correction.status_code, 201, correction.content)
        preview = self.preview(count_id=count.data["id"])
        result = self.client.post(self.path(), self.body(preview, count_id=count.data["id"]), format="json")
        self.assertEqual(result.status_code, 201, result.content)
        self.assertFalse(self.current().data["has_unresolved_shortage"])
        self.assertIsNone(self.current().data["stock_value_nanos_fcfa"])
        values = f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/stock-valuations/"
        price = self.client.post(values + "preview/", {"value_fcfa": 1200}, format="json")
        self.assertEqual(price.status_code, 200, price.content)
        confirmed = self.client.post(values, {**self.body(price), "value_fcfa": 1200}, format="json")
        self.assertEqual(confirmed.status_code, 201, confirmed.content)
        self.assertEqual(self.current().data["stock_value_nanos_fcfa"], "1200000000000")
        self.assertFalse(self.current().data["valuation_requires_regularization"])
        self.assertEqual(StockCostSnapshot.objects.get(movement__sale_line__sale_id=old_sale["id"]).cost_state, "provisional")
        self.assertEqual(StockMovement.objects.count(), 3)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 200)

    def test_cost_replay_preserves_physical_review_without_approving_old_sale_cost(self):
        sale, _ = self.shortage()
        reviewed = self.client.post(self.path(), self.body(self.preview()), format="json")
        self.assertEqual(reviewed.status_code, 201, reviewed.content)
        path = f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/cost-revaluations/"
        preview = self.client.get(path + "preview/")
        self.assertEqual(preview.status_code, 200, preview.content)
        result = self.client.post(path, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "reason": "Recalcul après contrôle physique", "preview_token": preview.data["preview_token"]}, format="json")
        self.assertEqual(result.status_code, 201, result.content)
        self.assertFalse(self.current().data["has_unresolved_shortage"])
        self.assertEqual(StockCostSnapshot.objects.filter(movement__sale_line__sale_id=sale["id"]).order_by("-version").first().cost_state,
            "provisional")
