"""US-12/15/33: persisted stock quantities and cost micros are exact integers."""
from decimal import Decimal

from django.db import connection, IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from catalogue.models import Product
from common.models import Activity
from identity.models import Shop, User
from stock.models import StockMovement
from stock.services.stock_queries import with_stock


class MovementStorageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="movement-storage")
        self.shop = Shop.objects.create(owner=self.owner, name="Stock exact")
        self.product = Product.objects.create(shop=self.shop, name="Riz", unit_kind="weight", unit_label="kg",
            sale_price_fcfa=1500, stock_threshold_milli=1000)

    def movement(self, quantity=1000, cost=None):
        kind = "loss" if quantity < 0 else "opening"
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="stock_loss_recorded" if quantity < 0 else "stock_opened",
            target_id=self.product.id, reason="Stock repris sans achat fictif")
        return StockMovement.objects.create(product=self.product, actor=self.owner, operation=event,
            kind=kind, quantity_milli=quantity, unit_cost_micros=cost, occurred_at=timezone.now())

    def test_sqlite_rejects_noninteger_quantities_instead_of_truncating_available_stock(self):
        movement = self.movement()
        for value in (1000.5, -0.5, 1e20, "inconnu"):
            with self.subTest(quantity=value), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("UPDATE core_stockmovement SET quantity_milli = %s WHERE id = %s", [value, movement.id.hex])
        available = with_stock(Product.objects.filter(id=self.product.id)).get()
        self.assertEqual(int(available.stock_available_milli), 1000)
        self.assertTrue(available.stock_low)

    def test_sqlite_rejects_fractional_text_overflow_and_negative_cost_micros(self):
        movement = self.movement(cost=600123456)
        for value in (600123456.5, 1e20, "inconnu", -1):
            with self.subTest(cost=value), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("UPDATE core_stockmovement SET unit_cost_micros = %s WHERE id = %s", [value, movement.id.hex])
        movement.refresh_from_db()
        self.assertEqual(movement.unit_cost_fcfa, Decimal("600.123456"))

    def test_unknown_zero_and_known_costs_stay_distinct_and_signed_quantities_survive(self):
        for quantity, cost, expected_cost in ((0, None, None), (-1, 0, Decimal("0")),
                (125, 600123456, Decimal("600.123456")),
                (9223372036854775807, 9223372036854775807, Decimal("9223372036854.775807"))):
            with self.subTest(quantity=quantity, cost=cost):
                movement = self.movement(quantity=quantity, cost=cost)
                movement.refresh_from_db()
                self.assertEqual(movement.quantity_milli, quantity)
                self.assertEqual(movement.unit_cost_fcfa, expected_cost)
