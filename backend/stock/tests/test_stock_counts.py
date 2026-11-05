"""US-13: physical observations stay fixed while theoretical stock can be revised."""
import uuid
from django.test import TestCase
from cash.models import CashMovement
from stock.models import StockMovement
from stock.tests import test_stock_losses


class StockCountTests(TestCase):
    setUp = test_stock_losses.StockLossTests.setUp
    authenticate = test_stock_losses.StockLossTests.authenticate
    product_payload = test_stock_losses.StockLossTests.product_payload
    prepare = test_stock_losses.StockLossTests.prepare

    def url(self, product):
        return f"/api/v1/shops/{self.shop.id}/products/{product['id']}/stock-counts/"

    def payload(self, physical=8000):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "counted_at": "2026-01-02T10:00:00Z",
                "physical_milli": physical, "reason": "Comptage physique"}

    def loss(self, product, at):
        result = self.client.post(f"/api/v1/shops/{self.shop.id}/products/{product['id']}/losses/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": at,
            "quantity_milli": 1000, "reason": "Perte réelle",
        }, format="json")
        self.assertEqual(result.status_code, 201)

    def test_observation_and_retry_preserve_physical_without_automatic_stock_correction(self):
        product = self.prepare()
        data = self.payload()
        result = self.client.post(self.url(product), data, format="json")
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.data["physical_milli"], 8000)
        self.assertEqual(result.data["expected_at_creation_milli"], 10000)
        self.assertEqual(result.data["expected_milli"], 10000)
        self.assertEqual(result.data["difference_milli"], -2000)
        self.assertTrue(result.data["provisional"])
        retry = self.client.post(self.url(product), data, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, result.data)
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertFalse(CashMovement.objects.exists())

    def test_post_count_operation_does_not_change_expected_at_count_but_prior_late_operation_does(self):
        product = self.prepare()
        result = self.client.post(self.url(product), self.payload(), format="json")
        self.assertEqual(result.status_code, 201)
        detail = self.url(product) + result.data["id"] + "/"
        self.loss(product, "2026-01-02T10:05:00Z")
        self.assertEqual(self.client.get(detail).data["expected_milli"], 10000)
        self.loss(product, "2026-01-02T09:50:00Z")
        revised = self.client.get(detail)
        self.assertEqual(revised.data["physical_milli"], 8000)
        self.assertEqual(revised.data["expected_at_creation_milli"], 10000)
        self.assertEqual(revised.data["expected_milli"], 9000)
        self.assertEqual(revised.data["difference_milli"], -1000)
        self.assertTrue(revised.data["expected_revised"])

    def test_seller_foreign_product_and_piece_fraction_have_no_count_or_stock_effect(self):
        product = self.prepare()
        self.assertEqual(self.client.post(self.url(product), self.payload(8500), format="json").status_code, 400)
        foreign = f"/api/v1/shops/{self.other.id}/products/{product['id']}/stock-counts/"
        self.assertEqual(self.client.post(foreign, self.payload(), format="json").status_code, 404)
        self.authenticate(self.seller)
        self.assertEqual(self.client.post(self.url(product), self.payload(), format="json").status_code, 403)
        self.assertEqual(self.client.get(self.url(product)).status_code, 403)
        self.assertEqual(StockMovement.objects.count(), 1)

    def confirmation(self, expected=10000, applied=0):
        return {"operation_id": str(uuid.uuid4()), "expected_milli": expected, "applied_adjustment_milli": applied,
                "reason": "Écart physique confirmé"}

    def test_confirmed_correction_keeps_post_count_sale_and_retries_without_double_effect(self):
        product = self.prepare()
        count = self.client.post(self.url(product), self.payload(), format="json")
        self.assertEqual(count.status_code, 201)
        sale = self.client.post(f"/api/v1/shops/{self.shop.id}/sales/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "lines": [{"id": str(uuid.uuid4()), "product_id": product["id"], "product_version": 1, "quantity_milli": 1000}],
            "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000}],
        }, format="json")
        self.assertEqual(sale.status_code, 201)
        data = self.confirmation()
        url = self.url(product) + count.data["id"] + "/confirm/"
        result = self.client.post(url, data, format="json")
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.data["delta_milli"], -2000)
        self.assertEqual(result.data["after_applied_adjustment_milli"], -2000)
        retry = self.client.post(url, data, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, result.data)
        self.assertEqual(sum(StockMovement.objects.values_list("quantity_milli", flat=True)), 7000)
        self.assertEqual(CashMovement.objects.count(), 1)
        detail = self.client.get(self.url(product) + count.data["id"] + "/")
        self.assertEqual(detail.data["expected_milli"], 10000)
        self.assertEqual(detail.data["proposed_adjustment_milli"], 0)

    def test_prior_late_operation_proposes_complementary_adjustment_without_automatic_double_deduction(self):
        product = self.prepare()
        count = self.client.post(self.url(product), self.payload(), format="json")
        self.assertEqual(count.status_code, 201)
        root = self.url(product) + count.data["id"] + "/"
        self.assertEqual(self.client.post(root + "confirm/", self.confirmation(), format="json").status_code, 201)
        self.loss(product, "2026-01-02T09:50:00Z")
        revised = self.client.get(root)
        self.assertEqual(revised.data["physical_milli"], 8000)
        self.assertEqual(revised.data["expected_milli"], 9000)
        self.assertEqual(revised.data["applied_adjustment_milli"], -2000)
        self.assertEqual(revised.data["proposed_adjustment_milli"], 1000)
        self.assertEqual(sum(StockMovement.objects.values_list("quantity_milli", flat=True)), 7000)
        stale = self.client.post(root + "confirm/", self.confirmation(10000, -2000), format="json")
        self.assertEqual(stale.status_code, 409)
        corrected = self.client.post(root + "confirm/", self.confirmation(9000, -2000), format="json")
        self.assertEqual(corrected.status_code, 201)
        self.assertEqual(corrected.data["delta_milli"], 1000)
        self.assertEqual(corrected.data["cost"]["quantity_after_milli"], "8000")
        self.assertEqual(sum(StockMovement.objects.values_list("quantity_milli", flat=True)), 8000)
        self.assertEqual(StockMovement.objects.filter(kind="inventory_adjustment").count(), 2)
        page = self.client.get(root + "adjustments/?page_size=1")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.data["count"], 2)
        self.assertEqual(len(page.data["results"]), 1)
        self.assertIsNotNone(page.data["next"])
        self.assertEqual(page.data["results"][0]["delta_milli"], -2000)
        self.assertFalse(CashMovement.objects.exists())

    def test_zero_difference_confirmation_creates_a_decision_without_fake_stock_movement(self):
        from stock.models import StockCountAdjustment
        product = self.prepare()
        count = self.client.post(self.url(product), self.payload(10000), format="json")
        self.assertEqual(count.status_code, 201)
        result = self.client.post(self.url(product) + count.data["id"] + "/confirm/", self.confirmation(), format="json")
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.data["delta_milli"], 0)
        self.assertIsNone(StockCountAdjustment.objects.get().movement_id)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_history_conserves_original_count_and_two_adjustments_with_motives(self):
        product = self.prepare()
        count = self.client.post(self.url(product), self.payload(), format="json")
        self.assertEqual(count.status_code, 201)
        root = self.url(product) + count.data["id"] + "/"
        first = self.confirmation()
        self.assertEqual(self.client.post(root + "confirm/", first, format="json").status_code, 201)
        self.loss(product, "2026-01-02T09:50:00Z")
        second = self.confirmation(9000, -2000)
        second["reason"] = "Rapprochement de la perte tardive"
        self.assertEqual(self.client.post(root + "confirm/", second, format="json").status_code, 201)
        history = self.client.get(root + "history/")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.data["count"], 3)
        self.assertEqual([row["action"] for row in history.data["results"]], ["stock_count_recorded", "stock_count_adjusted", "stock_count_adjusted"])
        self.assertEqual(history.data["results"][1]["details"]["response"]["delta_milli"], -2000)
        self.assertEqual(history.data["results"][2]["details"]["response"]["delta_milli"], 1000)
        self.assertEqual(history.data["results"][2]["reason"], second["reason"])
        self.assertEqual(self.client.post(self.url(product), {**self.payload(), "id": count.data["id"]}, format="json").status_code, 409)

    def test_confirmation_requires_reason_and_rechecks_session_before_effect_or_retry(self):
        from unittest.mock import patch
        from django.utils import timezone
        from identity.models import DeviceSession
        from stock.models import StockCountAdjustment
        from stock.api.stock_counts import confirm_count
        product = self.prepare()
        count = self.client.post(self.url(product), self.payload(), format="json")
        self.assertEqual(count.status_code, 201)
        root = self.url(product) + count.data["id"] + "/"
        self.assertEqual(self.client.post(root + "confirm/", {**self.confirmation(), "reason": ""}, format="json").status_code, 400)
        data = self.confirmation()
        self.assertEqual(self.client.post(root + "confirm/", data, format="json").status_code, 201)
        def revoked(*args, **kwargs):
            DeviceSession.objects.filter(installation__user=self.owner).update(revoked_at=timezone.now())
            return confirm_count(*args, **kwargs)
        with patch("stock.api.stock_counts.confirm_count", side_effect=revoked):
            retry = self.client.post(root + "confirm/", data, format="json")
        self.assertEqual(retry.status_code, 401)
        self.assertEqual(StockCountAdjustment.objects.count(), 1)
        self.assertEqual(StockMovement.objects.get(kind="inventory_adjustment").quantity_milli, -2000)

    def test_count_model_keeps_integer_observations_and_piece_precision(self):
        from django.db import IntegrityError, transaction
        from stock.models import StockCount
        product = self.prepare()
        result = self.client.post(self.url(product), self.payload(), format="json")
        self.assertEqual(result.status_code, 201)
        for changes in ({"physical_milli": 8500}, {"unit_kind": "unknown"},
                        {"expected_at_creation_milli": 10000.5}, {"expected_at_creation_milli": True}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                StockCount.objects.update(**changes)
