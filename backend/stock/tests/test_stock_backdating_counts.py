"""US-35 / G15: a loss dated before a validated inventory count of the product must name that count."""
import uuid

from django.test import TestCase

from stock.tests import test_stock_counts


class LossBeforeCountTests(TestCase):
    setUp = test_stock_counts.StockCountTests.setUp
    authenticate = test_stock_counts.StockCountTests.authenticate
    product_payload = test_stock_counts.StockCountTests.product_payload
    prepare = test_stock_counts.StockCountTests.prepare
    url = test_stock_counts.StockCountTests.url
    payload = test_stock_counts.StockCountTests.payload

    def post_loss(self, product, at, **changes):
        return self.client.post(f"/api/v1/shops/{self.shop.id}/products/{product['id']}/losses/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": at,
            "backdate_reason": "Reprise historique", "quantity_milli": 1000, "reason": "Perte réelle", **changes}, format="json")

    def test_loss_before_count_requires_naming_it_and_after_count_does_not(self):
        product = self.prepare()
        count = self.payload()
        self.assertEqual(self.client.post(self.url(product), count, format="json").status_code, 201)
        self.assertEqual(self.post_loss(product, "2026-01-02T09:50:00Z").status_code, 400)
        self.assertEqual(self.post_loss(product, "2026-01-02T09:50:00Z", revises_count_id=str(uuid.uuid4())).status_code, 400)
        self.assertEqual(self.post_loss(product, "2026-01-02T09:50:00Z", revises_count_id=count["id"]).status_code, 201)
        self.assertEqual(self.post_loss(product, "2026-01-02T10:05:00Z").status_code, 201)
