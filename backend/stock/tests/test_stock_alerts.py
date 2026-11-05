"""US-15: known stock at or below the optional threshold is visible."""
import uuid
from django.test import TestCase
from django.utils import timezone
from common.models import Activity
from catalogue.models import Product
from stock.models import StockMovement
from stock.tests import test_stock_thresholds as threshold_tests


class StockAlertApiTests(TestCase):
    setUp = threshold_tests.StockThresholdApiTests.setUp
    authenticate = threshold_tests.StockThresholdApiTests.authenticate
    payload = threshold_tests.StockThresholdApiTests.payload
    create = threshold_tests.StockThresholdApiTests.create
    url = threshold_tests.StockThresholdApiTests.url

    def movement(self, product_id, quantity):
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="stock_fixture",
                                        target_id=product_id, reason="Arrivée connue")
        return StockMovement.objects.create(product_id=product_id, actor=self.owner, operation=event,
                                           kind="purchase_receipt", quantity_milli=quantity,
                                           occurred_at=timezone.now(), reason="Arrivée connue")

    def alerts(self, **params):
        return self.client.get(f"/api/v1/shops/{self.shop.id}/stock-alerts/", params)

    def test_indicator_includes_equality_negative_and_zero_but_not_missing_threshold(self):
        for quantity, threshold, expected in [(8000, 8000, True), (9000, 8000, False),
                                               (-1000, 0, True), (0, 0, True), (0, None, False)]:
            data, _ = self.create(stock_threshold_milli=threshold)
            self.movement(data["id"], quantity)
            result = self.client.get(self.url(data["id"]))
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.data["stock_available_milli"], quantity)
            self.assertEqual(result.data["stock_reserved_milli"], 0)
            self.assertEqual(result.data["is_stock_low"], expected)
            self.assertTrue(result.data["stock_provisional"])
            self.assertIn("stock_as_of", result.data)
        self.authenticate(self.seller)
        result = self.client.get(self.url(data["id"]))
        self.assertIn("is_stock_low", result.data)
        self.member.can_read_cost = False
        self.member.save()
        self.assertNotIn("purchase_price_fcfa", self.client.get(self.url(data["id"])).data)

    def test_manager_center_is_filtered_paginated_and_excludes_archived_and_foreign_products(self):
        active_ids = []
        for name in ("A", "B", "C"):
            data, _ = self.create(name=name, stock_threshold_milli=8000)
            self.movement(data["id"], 8000)
            active_ids.append(data["id"])
        self.create(name="Sans seuil")
        above, _ = self.create(stock_threshold_milli=8000)
        self.movement(above["id"], 9000)
        archived, _ = self.create(stock_threshold_milli=8000)
        Product.objects.filter(id=archived["id"]).update(is_active=False)
        Product.objects.create(shop=self.other, name="Étranger", unit_kind="piece", unit_label="pièce",
                               sale_price_fcfa=250, stock_threshold_milli=8000)
        self.authenticate(self.manager)
        result = self.alerts(page_size=2)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["count"], 3)
        self.assertEqual(len(result.data["results"]), 2)
        second = self.client.get(result.data["next"])
        self.assertEqual([row["id"] for row in result.data["results"] + second.data["results"]], active_ids)
        self.authenticate(self.seller)
        self.assertEqual(self.alerts().status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.other.id}/stock-alerts/").status_code, 404)

    def test_arrival_above_threshold_removes_alert_and_later_loss_restores_it(self):
        data, _ = self.create(stock_threshold_milli=8000)
        self.movement(data["id"], 8000)
        self.assertEqual(self.alerts().data["count"], 1)
        self.movement(data["id"], 1000)
        self.assertEqual(self.alerts().data["count"], 0)
        self.movement(data["id"], -1000)
        self.assertEqual(self.alerts().data["count"], 1)
        self.assertEqual(self.alerts().data["count"], 1)

    def test_pending_operation_reserves_once_and_validation_does_not_double_deduct(self):
        from stock.models import StockReservation
        data, _ = self.create(stock_threshold_milli=8000)
        self.movement(data["id"], 10000)
        event = Activity.objects.create(shop=self.shop, actor=self.seller, action="pending_sale_fixture",
                                        target_id=data["id"], reason="En attente")
        reservation = StockReservation.objects.create(product_id=data["id"], operation=event, quantity_milli=2000)
        result = self.client.get(self.url(data["id"]))
        self.assertEqual(result.data["stock_validated_milli"], 10000)
        self.assertEqual(result.data["stock_reserved_milli"], 2000)
        self.assertEqual(result.data["stock_available_milli"], 8000)
        self.assertTrue(result.data["is_stock_low"])
        self.assertEqual(self.alerts().data["count"], 1)
        StockMovement.objects.create(product_id=data["id"], actor=self.owner, operation=event,
                                    kind="sale", quantity_milli=-2000, occurred_at=timezone.now(), reason="Validé")
        reservation.state = "validated"
        reservation.save()
        validated = self.client.get(self.url(data["id"]))
        self.assertEqual(validated.data["stock_available_milli"], 8000)
        self.assertEqual(validated.data["stock_reserved_milli"], 0)
        self.assertEqual(self.alerts().data["count"], 1)

    def test_rejection_keeps_reservation_until_physical_departure_is_explicitly_regularized(self):
        from stock.models import StockReservation
        data, _ = self.create(stock_threshold_milli=8000)
        self.movement(data["id"], 10000)
        event = Activity.objects.create(shop=self.shop, actor=self.seller, action="pending_sale_fixture",
                                        target_id=data["id"], reason="En attente")
        reservation = StockReservation.objects.create(product_id=data["id"], operation=event, quantity_milli=2000)
        self.assertEqual(self.alerts().data["count"], 1)
        reservation.state = "rejected"
        reservation.save()
        self.assertEqual(self.alerts().data["count"], 1)
        row = self.client.get(self.url(data["id"])).data
        self.assertEqual(row["stock_available_milli"], 8000)
        self.assertEqual(row["stock_reserved_milli"], 2000)
        self.assertEqual(row["stock_pending_reserved_milli"], 0)
        self.assertEqual(row["stock_rejected_reserved_milli"], 2000)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_pending_and_rejected_claims_are_distinct_without_reopening_an_alert_episode(self):
        from stock.models import StockReservation, StockAlertEvent
        from stock.services.stock_alert_services import observe_stock
        data, _ = self.create(stock_threshold_milli=8000)
        self.movement(data["id"], 10000)
        operation = Activity.objects.create(shop=self.shop, actor=self.seller, action="pending_sale_fixture",
            target_id=data["id"], reason="Vente en attente")
        reservation = StockReservation.objects.create(product_id=data["id"], operation=operation, quantity_milli=2000)
        observe_stock([uuid.UUID(data["id"])], operation)
        before = self.client.get(self.url(data["id"])).data
        self.assertEqual(before["stock_pending_reserved_milli"], 2000)
        self.assertEqual(before["stock_rejected_reserved_milli"], 0)
        reservation.state = "rejected"
        reservation.save()
        rejection = Activity.objects.create(shop=self.shop, actor=self.owner, action="pending_sale_rejected",
            target_id=data["id"], reason="Refus sans restitution physique")
        observe_stock([uuid.UUID(data["id"])], rejection)
        second_operation = Activity.objects.create(shop=self.shop, actor=self.seller, action="pending_sale_fixture",
            target_id=data["id"], reason="Autre vente en attente")
        StockReservation.objects.create(product_id=data["id"], operation=second_operation, quantity_milli=2000)
        observe_stock([uuid.UUID(data["id"])], second_operation)
        row = self.client.get(self.url(data["id"])).data
        self.assertEqual(row["stock_available_milli"], 6000)
        self.assertEqual(row["stock_reserved_milli"], 4000)
        self.assertEqual(row["stock_pending_reserved_milli"], 2000)
        self.assertEqual(row["stock_rejected_reserved_milli"], 2000)
        self.assertEqual(StockAlertEvent.objects.filter(product_id=data["id"]).count(), 1)
        self.assertEqual(StockMovement.objects.count(), 1)
        from cash.models import CashMovement
        self.assertFalse(CashMovement.objects.exists())

    def test_catalogue_update_receipt_uses_pending_reservations(self):
        from stock.models import StockReservation
        import uuid
        data, _ = self.create(stock_threshold_milli=8000)
        self.movement(data["id"], 10000)
        event = Activity.objects.create(shop=self.shop, actor=self.seller, action="pending_sale_fixture",
                                        target_id=data["id"], reason="En attente")
        StockReservation.objects.create(product_id=data["id"], operation=event, quantity_milli=2000)
        changed = self.client.patch(self.url(data["id"]), {"operation_id": str(uuid.uuid4()), "base_version": 1,
            "reason": "Nouveau seuil", "stock_threshold_milli": 9000}, format="json")
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.data["stock_available_milli"], 8000)
        self.assertEqual(changed.data["stock_reserved_milli"], 2000)
        self.assertTrue(changed.data["is_stock_low"])

    def test_rejected_claim_totals_beyond_int64_are_exact_and_visible_to_seller(self):
        from stock.models import StockReservation
        data, _ = self.create(unit_kind="weight", stock_threshold_milli=0)
        for index in range(2):
            event = Activity.objects.create(shop=self.shop, actor=self.seller, action="pending_sale_fixture",
                target_id=data["id"], reason=f"Vente refusée {index}")
            StockReservation.objects.create(product_id=data["id"], operation=event,
                quantity_milli=9223372036854775807, state="rejected")
        self.authenticate(self.seller)
        response = self.client.get(self.url(data["id"]))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["stock_reserved_milli"], 18446744073709551614)
        self.assertEqual(response.data["stock_rejected_reserved_milli"], 18446744073709551614)
        self.assertEqual(response.data["stock_pending_reserved_milli"], 0)
        self.assertEqual(response.data["stock_available_milli"], -18446744073709551614)
        self.assertTrue(response.data["is_stock_low"])

    def test_stock_totals_beyond_sqlite_int64_stay_exact_and_filter_correctly(self):
        data, _ = self.create(unit_kind="weight", stock_threshold_milli=0)
        self.movement(data["id"], 9223372036854775807)
        self.movement(data["id"], 9223372036854775807)
        result = self.client.get(self.url(data["id"]))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["stock_available_milli"], 18446744073709551614)
        self.assertFalse(result.data["is_stock_low"])
        self.assertEqual(self.alerts().data["count"], 0)

    def test_all_rows_in_one_alert_response_share_observation_time(self):
        for name in ("A", "B"):
            self.create(name=name, stock_threshold_milli=0)
        response = self.alerts()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"][0]["stock_as_of"], response.data["results"][1]["stock_as_of"])

    def test_real_sale_and_retry_trigger_one_known_low_stock_indicator(self):
        import uuid
        data, _ = self.create(stock_threshold_milli=8000)
        self.movement(data["id"], 10000)
        self.authenticate(self.seller)
        sale = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "lines": [{"id": str(uuid.uuid4()), "product_id": data["id"],
                           "product_version": 1, "quantity_milli": 2000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 500}]}
        endpoint = f"/api/v1/shops/{self.shop.id}/sales/"
        first = self.client.post(endpoint, sale, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        replay = self.client.post(endpoint, sale, format="json")
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(first.data, replay.data)
        product = self.client.get(self.url(data["id"]))
        self.assertEqual(product.data["stock_available_milli"], 8000)
        self.assertTrue(product.data["is_stock_low"])
        self.assertEqual(StockMovement.objects.filter(kind="sale").count(), 1)
        self.assertEqual(self.alerts().status_code, 403)

    def test_reservation_uniqueness_and_database_validation(self):
        from django.db import IntegrityError, connection, transaction
        from stock.models import StockReservation
        data, _ = self.create()
        event = Activity.objects.create(shop=self.shop, actor=self.seller, action="pending_sale_fixture",
                                        target_id=data["id"], reason="En attente")
        StockReservation.objects.create(product_id=data["id"], operation=event, quantity_milli=2000)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StockReservation.objects.create(product_id=data["id"], operation=event, quantity_milli=2000)
        for value in (0, -1000, 1000.5):
            with self.subTest(value=value), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("UPDATE core_stockreservation SET quantity_milli = %s", [value])
        with self.assertRaises(IntegrityError), transaction.atomic():
            StockReservation.objects.update(state="unknown")

    def test_alert_filtering_and_limit_are_executed_in_sql(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        for name in ("A", "B", "C"):
            self.create(name=name, stock_threshold_milli=0)
        with CaptureQueriesContext(connection) as captured:
            result = self.alerts(page_size=1)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["count"], 3)
        rows = [query["sql"] for query in captured.captured_queries
                if 'FROM "core_product"' in query["sql"] and "LIMIT 1" in query["sql"]]
        self.assertTrue(rows)
        self.assertTrue(any("stock_at_threshold" in sql for sql in rows))
