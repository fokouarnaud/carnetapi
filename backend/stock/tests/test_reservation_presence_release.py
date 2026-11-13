"""US-37 / G19: a refused reservation is released on observed presence, without any stock movement."""
from django.db import IntegrityError, transaction
from django.test import TestCase

from catalogue.models import Product
from common.models import Activity
from identity.models import Installation, Shop, User
from stock.models import StockMovement, StockReservation
from stock.services.stock_queries import with_stock


class ReservationPresenceReleaseTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="presence-owner")
        self.shop = Shop.objects.create(owner=self.owner, name="A")
        self.installation = Installation.objects.create(user=self.owner)
        self.product = Product.objects.create(shop=self.shop, name="Savon", unit_kind="piece", unit_label="pièce", sale_price_fcfa=100)

    def event(self, action):
        return Activity.objects.create(shop=self.shop, actor=self.owner, action=action, target_id=self.product.id, reason="Contrôle")

    def release(self, reservation, quantity):
        from stock.models import StockReservationPresenceRelease
        return StockReservationPresenceRelease.objects.create(
            reservation=reservation, operation=self.event("reservation_presence_released"),
            installation=self.installation, quantity_milli=quantity, reason="Marchandise présente en rayon")

    def stock(self):
        return with_stock(Product.objects.filter(id=self.product.id)).get()

    def test_presence_release_restores_availability_without_movement(self):
        reservation = StockReservation.objects.create(product=self.product, operation=self.event("sale_refused"),
                                                      quantity_milli=3000, state="rejected")
        self.assertEqual(self.stock().stock_available_milli, "-3000")
        self.release(reservation, 2000)
        stock = self.stock()
        self.assertEqual((stock.stock_validated_milli, stock.stock_available_milli, stock.stock_rejected_reserved_milli),
                         ("0", "-1000", "1000"))
        self.assertFalse(StockMovement.objects.exists())

    def test_release_quantity_must_be_positive(self):
        reservation = StockReservation.objects.create(product=self.product, operation=self.event("sale_refused"),
                                                      quantity_milli=1000, state="rejected")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.release(reservation, 0)
