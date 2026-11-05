"""Pending physical departures survive copies and never count as validated cash."""
import uuid

from django.db import IntegrityError,transaction,connection
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from backups import models as backups
from backups.tests import test_recovery_integration_models as fixtures
from common.models import Activity
from stock.models import StockReservation
from stock.services.stock_queries import with_stock


class RecoverySaleHoldModelTests(TestCase):
    setUp=fixtures.RecoveryIntegrationModelTests.setUp
    generation=fixtures.RecoveryIntegrationModelTests.generation
    record=fixtures.RecoveryIntegrationModelTests.record
    deposit=fixtures.RecoveryIntegrationModelTests.deposit
    capture=fixtures.RecoveryIntegrationModelTests.capture

    def hold(self,**changes):
        capture=self.capture(self.deposit())
        event=Activity.objects.create(shop=self.a,actor=self.owner,action="recovery_sale_pending",target_id=capture.id,reason="Contrôle attendu")
        return backups.RecoverySaleHold.objects.create(**{"capture":capture,"source_operation_id":capture.operation_id,"sale_id":uuid.uuid4(),
            "reservation_operation":event,"net_total_fcfa":500,"payments_fcfa":200,**changes})

    def decision(self,hold,**changes):
        return backups.RecoverySaleHoldDecision.objects.create(**{"hold":hold,"capture":hold.capture,
            "operation_id":uuid.uuid4(),"reviewed_by":self.owner,"reviewed_installation":self.installation,
            "previous_version":0,"version":1,"state":"pending","reason":"Origine à contrôler",
            "request":{},"receipt":{},**changes})

    def test_one_source_hold_reserves_once_and_keeps_pending_money_separate(self):
        from catalogue.models import Product
        from cash.models import CashMovement
        from sales.models import Sale
        product=Product.objects.create(shop=self.a,name="Pièce",unit_kind="piece",unit_label="pièce",sale_price_fcfa=500)
        hold=self.hold()
        StockReservation.objects.create(product=product,operation=hold.reservation_operation,quantity_milli=1000)
        self.decision(hold)
        hold.refresh_from_db()
        stock=with_stock(Product.objects.filter(id=product.id)).get()
        self.assertEqual((stock.stock_validated_milli,stock.stock_reserved_milli,stock.stock_available_milli),("0","1000","-1000"))
        self.assertEqual((hold.net_total_fcfa,hold.payments_fcfa),(500,200))
        self.assertEqual((Sale.objects.count(),CashMovement.objects.count()),(0,0))
        with self.assertRaises(IntegrityError),transaction.atomic():
            backups.RecoverySaleHold.objects.create(capture=hold.capture,source_operation_id=hold.source_operation_id,
                sale_id=uuid.uuid4(),reservation_operation=hold.reservation_operation,net_total_fcfa=500,payments_fcfa=200)
        for changes in ({"sale_id":hold.sale_id},{"source_operation_id":hold.source_operation_id}):
            with self.subTest(identity=tuple(changes)),self.assertRaises(IntegrityError),transaction.atomic():self.hold(**changes)
        with self.assertRaises(ProtectedError):hold.capture.delete()

    def test_decisions_are_versioned_and_refusal_does_not_delete_the_source(self):
        hold=self.hold()
        first=self.decision(hold)
        refused=self.decision(hold,previous_version=1,version=2,state="refused",reason="Écart à régulariser")
        self.assertEqual(list(hold.decisions.values_list("state",flat=True)),["pending","refused"])
        with self.assertRaises(IntegrityError),transaction.atomic():self.decision(hold)
        with self.assertRaises(IntegrityError),transaction.atomic():
            self.decision(hold,operation_id=first.operation_id,previous_version=2,version=3)
        self.assertTrue(backups.RecoveryJournalOperation.objects.filter(id=hold.capture_id).exists())
        with self.assertRaises(ProtectedError):refused.hold.delete()

    def test_invalid_projection_money_versions_and_unlinked_validation_are_rejected(self):
        for changes in ({"state":"forgotten"},{"net_total_fcfa":-1},{"payments_fcfa":-1}):
            with self.subTest(projection=changes),self.assertRaises(IntegrityError),transaction.atomic():self.hold(**changes)
        hold=self.hold()
        for field in ("version","net_total_fcfa","payments_fcfa"):
            with self.subTest(fractional=field),self.assertRaises(IntegrityError),transaction.atomic(),connection.cursor() as cursor:
                cursor.execute(f"UPDATE backups_recoverysalehold SET {field}=1.5 WHERE id=%s",[hold.id.hex])
        for changes in ({"version":0},{"previous_version":1,"version":1},{"state":"validated"},
                        {"state":"forgotten"},{"reason":" "},{"request":[]},{"receipt":[]}):
            with self.subTest(decision=changes),self.assertRaises(IntegrityError),transaction.atomic():self.decision(hold,**changes)
