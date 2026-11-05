"""Crossing detection runs on completed API writes, independently of reads."""
import uuid
from django.test import TestCase
from common.models import Activity
from stock.models import StockMovement
from stock.tests import test_stock_thresholds as threshold_tests


class StockCrossingApiTests(TestCase):
    setUp = threshold_tests.StockThresholdApiTests.setUp
    authenticate = threshold_tests.StockThresholdApiTests.authenticate
    payload = threshold_tests.StockThresholdApiTests.payload
    create = threshold_tests.StockThresholdApiTests.create
    url = threshold_tests.StockThresholdApiTests.url

    def prepare(self):
        data, _ = self.create(stock_threshold_milli=8000, opening={
            "quantity_milli": 10000, "unit_cost_fcfa": None,
            "occurred_at": "2026-01-01T10:00:00Z", "reason": "Reprise"})
        return data

    def sale(self, product, quantity=2000):
        body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "lines": [{"id": str(uuid.uuid4()), "product_id": product["id"],
                           "product_version": 1, "quantity_milli": quantity}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": quantity // 4}]}
        endpoint = f"/api/v1/shops/{self.shop.id}/sales/"
        response = self.client.post(endpoint, body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return body, endpoint

    def test_sale_crossing_and_retry_emit_one_event_without_requiring_catalogue_read(self):
        from stock.models import StockAlertEvent
        data = self.prepare()
        self.authenticate(self.seller)
        body, endpoint = self.sale(data)
        self.assertEqual(StockAlertEvent.objects.count(), 1)
        event = StockAlertEvent.objects.get()
        self.assertEqual(event.available_milli, 8000)
        self.assertEqual(event.operation_id, uuid.UUID(body["operation_id"]))
        self.assertEqual(self.client.post(endpoint, body, format="json").status_code, 200)
        self.sale(data, 1000)
        self.assertEqual(StockAlertEvent.objects.count(), 1)

    def test_creation_observes_final_opening_and_threshold_update_is_audited_crossing(self):
        from stock.models import StockAlertEvent, StockAlertState
        data = self.prepare()
        self.assertEqual(StockAlertEvent.objects.count(), 0)
        self.assertFalse(StockAlertState.objects.get(product_id=data["id"]).is_low)
        body = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                "reason": "Seuil haut", "stock_threshold_milli": 10000}
        response = self.client.patch(self.url(data["id"]), body, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StockAlertEvent.objects.get().operation_id, uuid.UUID(body["operation_id"]))
        self.assertEqual(self.client.patch(self.url(data["id"]), body, format="json").status_code, 200)
        self.assertEqual(StockAlertEvent.objects.count(), 1)
        self.create(stock_threshold_milli=0)
        self.assertEqual(StockAlertEvent.objects.count(), 2)

    def test_inventory_confirmation_rearms_and_subsequent_loss_emits_second_event(self):
        from stock.models import StockAlertEvent
        data = self.prepare()
        self.sale(data)
        root = self.url(data["id"])
        count = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "physical_milli": 9000,
                 "counted_at": "2026-10-01T10:00:00Z", "reason": "Comptage"}
        # Sale is later than the counted instant; choose a current effective instant.
        from django.utils import timezone
        count["counted_at"] = timezone.now().isoformat()
        created = self.client.post(root + "stock-counts/", count, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        confirmation = {"operation_id": str(uuid.uuid4()), "expected_milli": 8000,
                        "applied_adjustment_milli": 0, "reason": "Écart réel confirmé"}
        result = self.client.post(root + f"stock-counts/{count['id']}/confirm/", confirmation, format="json")
        self.assertEqual(result.status_code, 201, result.data)
        loss = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "quantity_milli": 1000,
                "occurred_at": timezone.now().isoformat(), "reason": "Casse"}
        result = self.client.post(root + "losses/", loss, format="json")
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(list(StockAlertEvent.objects.order_by("episode").values_list("episode", flat=True)), [1, 2])
        self.assertEqual(self.client.post(root + "losses/", loss, format="json").status_code, 200)
        self.assertEqual(StockAlertEvent.objects.count(), 2)

    def test_one_multiline_sale_snapshots_final_stock_and_one_episode(self):
        from stock.models import StockAlertEvent
        data = self.prepare()
        body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "lines": [{"id": str(uuid.uuid4()), "product_id": data["id"],
                           "product_version": 1, "quantity_milli": 2000} for _ in range(2)],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000}]}
        response = self.client.post(f"/api/v1/shops/{self.shop.id}/sales/", body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(StockAlertEvent.objects.count(), 1)
        self.assertEqual(StockAlertEvent.objects.get().available_milli, 6000)


from purchasing.tests import test_purchases as purchase_tests


class PurchaseCrossingApiTests(TestCase):
    setUp = purchase_tests.PurchaseTests.setUp
    authenticate = purchase_tests.PurchaseTests.authenticate
    url = purchase_tests.PurchaseTests.url
    payload = purchase_tests.PurchaseTests.payload

    def test_partial_receipt_rearms_and_supplier_departure_crosses_again(self):
        from stock.models import StockAlertEvent
        from purchasing.models import SupplierDebt
        self.product.stock_threshold_milli = 48000
        self.product.save()
        invoice = self.client.post(self.url(), self.payload(), format="json")
        self.assertEqual(invoice.status_code, 201, invoice.data)
        root = self.url() + invoice.data["id"] + "/"
        def quantity(amount):
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                    "occurred_at": "2026-01-02T10:00:00Z", "reason": "Mouvement physique",
                    "lines": [{"line_id": invoice.data["lines"][0]["id"], "quantity_milli": amount}]}
        two = quantity(2000)
        self.assertEqual(self.client.post(root + "receipts/", two, format="json").status_code, 201)
        self.assertEqual(StockAlertEvent.objects.get().available_milli, 48000)
        self.assertEqual(self.client.post(root + "receipts/", two, format="json").status_code, 200)
        self.assertEqual(self.client.post(root + "receipts/", quantity(1000), format="json").status_code, 201)
        returned = quantity(1000)
        self.assertEqual(self.client.post(root + "returns/", returned, format="json").status_code, 201)
        self.assertEqual(self.client.post(root + "returns/", returned, format="json").status_code, 200)
        self.assertEqual(list(StockAlertEvent.objects.order_by("episode").values_list("episode", flat=True)), [1, 2])
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 10000)
