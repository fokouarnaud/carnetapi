"""A refused quantity can be released without rewriting its physical history."""
import uuid
from django.apps import apps
from django.db import IntegrityError, transaction, connection
from django.test import TestCase
from django.utils import timezone
from catalogue.models import Product
from common.models import Activity
from identity.models import User, Shop, Installation
from stock.models import StockReservation, StockMovement
from stock.services.stock_queries import with_stock


class ReservationReconciliationModelTests(TestCase):
    def test_partial_reconciliation_releases_only_its_quantity_and_preserves_both_sources(self):
        owner=User.objects.create_user(username="stock-reconciliation-owner")
        shop=Shop.objects.create(owner=owner,name="A")
        installation=Installation.objects.create(user=owner)
        product=Product.objects.create(shop=shop,name="Pièce",unit_kind="piece",unit_label="pièce",sale_price_fcfa=100)
        def event(action):
            return Activity.objects.create(shop=shop,actor=owner,action=action,target_id=product.id,reason="Contrôle physique")
        reservation=StockReservation.objects.create(product=product,operation=event("recovery_sale_refused"),quantity_milli=3000,state="rejected")
        movement=StockMovement.objects.create(product=product,actor=owner,operation=event("stock_loss"),kind="loss",
            quantity_milli=-2000,occurred_at=timezone.now(),reason="Perte constatée")
        model=apps.all_models["stock"].get("stockreservationreconciliation")
        self.assertIsNotNone(model,"A durable reconciliation must link the reservation to the already counted movement")
        reconciliation=model.objects.create(reservation=reservation,movement=movement,operation=event("reservation_reconciled"),
            installation=installation,quantity_milli=1000)
        stock=with_stock(Product.objects.filter(id=product.id)).get()
        self.assertEqual((stock.stock_validated_milli,stock.stock_reserved_milli,stock.stock_available_milli), ("-2000","2000","-4000"))
        self.assertEqual((stock.stock_rejected_reserved_milli,stock.stock_pending_reserved_milli),("2000","0"))
        reservation.refresh_from_db();movement.refresh_from_db()
        self.assertEqual((reservation.quantity_milli,reservation.state,movement.quantity_milli),(3000,"rejected",-2000))
        self.assertEqual(StockMovement.objects.count(),1)
        with self.assertRaises(IntegrityError),transaction.atomic():
            model.objects.create(reservation=reservation,movement=movement,operation=reconciliation.operation,
                installation=installation,quantity_milli=1000)
        for quantity in (0,-1):
            with self.subTest(quantity=quantity),self.assertRaises(IntegrityError),transaction.atomic():
                model.objects.create(reservation=reservation,movement=movement,operation=event("reservation_reconciled"),
                    installation=installation,quantity_milli=quantity)
        with self.assertRaises(IntegrityError),transaction.atomic(),connection.cursor() as cursor:
            cursor.execute("UPDATE stock_stockreservationreconciliation SET quantity_milli=1.5 WHERE id=%s",[reconciliation.id.hex])
