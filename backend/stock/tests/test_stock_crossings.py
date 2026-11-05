"""US-15: a known low-stock episode produces one durable event."""
from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from common.models import Activity
from catalogue.models import Product
from identity.models import Shop, User
from stock.models import StockMovement


class StockCrossingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="crossing-owner")
        self.shop = Shop.objects.create(name="Franchissements", owner=self.owner)
        self.product = Product.objects.create(shop=self.shop, name="Savon", unit_kind="piece", unit_label="pièce",
                                              sale_price_fcfa=250, stock_threshold_milli=8000)

    def operation(self):
        return Activity.objects.create(shop=self.shop, actor=self.owner, action="stock_fixture",
                                        target_id=self.product.id, reason="Stock connu")

    def change(self, quantity):
        operation = self.operation()
        StockMovement.objects.create(product=self.product, actor=self.owner, operation=operation, kind="purchase_receipt",
                                     quantity_milli=quantity, occurred_at=timezone.now(), reason="Stock connu")
        self.observe(operation)
        return operation

    def observe(self, operation):
        from stock.services.stock_alert_services import observe_stock
        return observe_stock([self.product.id], operation)

    def test_one_event_at_equality_and_no_repeat_while_stock_stays_low(self):
        from stock.models import StockAlertEvent, StockAlertState
        self.change(10000)
        self.assertEqual(StockAlertEvent.objects.count(), 0)
        crossing = self.change(-2000)
        event = StockAlertEvent.objects.get()
        self.assertEqual(event.available_milli, 8000)
        self.assertEqual(event.threshold_milli, 8000)
        self.assertEqual(event.operation_id, crossing.id)
        self.assertEqual(event.episode, 1)
        self.change(-1000)
        self.observe(crossing)
        self.assertEqual(StockAlertEvent.objects.count(), 1)
        state = StockAlertState.objects.get(product=self.product)
        self.assertTrue(state.is_low)

    def test_above_threshold_rearms_and_later_crossing_has_new_stable_event(self):
        from stock.models import StockAlertEvent
        self.change(8000)
        first = StockAlertEvent.objects.get()
        self.change(1000)
        second_operation = self.change(-1000)
        events = list(StockAlertEvent.objects.order_by("episode"))
        self.assertEqual([event.episode for event in events], [1, 2])
        self.assertNotEqual(events[0].id, events[1].id)
        self.assertEqual(events[0].id, first.id)
        self.assertEqual(events[1].operation_id, second_operation.id)
        self.observe(second_operation)
        self.assertEqual(StockAlertEvent.objects.count(), 2)

    def test_disabling_threshold_or_archiving_clears_state_without_deleting_history(self):
        from stock.models import StockAlertEvent, StockAlertState
        self.change(8000)
        self.product.stock_threshold_milli = None
        self.product.save()
        self.observe(self.operation())
        self.assertFalse(StockAlertState.objects.get(product=self.product).is_low)
        self.assertEqual(StockAlertEvent.objects.count(), 1)
        self.product.stock_threshold_milli = 8000
        self.product.is_active = False
        self.product.save()
        self.observe(self.operation())
        self.assertEqual(StockAlertEvent.objects.count(), 1)

    def test_failure_rolls_back_stock_state_and_event_together(self):
        from stock.models import StockAlertEvent, StockAlertState
        self.change(10000)
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.change(-2000)
                raise RuntimeError("Écriture échouée")
        self.assertEqual(StockAlertEvent.objects.count(), 0)
        self.assertFalse(StockAlertState.objects.get(product=self.product).is_low)
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_sqlite_cannot_store_a_fractional_episode_counter(self):
        from django.db import connection, IntegrityError
        self.change(10000)
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("UPDATE core_stockalertstate SET episode = %s", [1.5])
