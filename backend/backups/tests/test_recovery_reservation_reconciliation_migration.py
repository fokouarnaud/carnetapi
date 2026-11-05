"""A new reconciliation table preserves refusals, physical facts and permissions."""
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class RecoveryReservationReconciliationMigrationTests(TransactionTestCase):
    def snapshot(self):
        tables=("core_user","core_shop","core_installation","core_product","core_activity",
                "core_stockmovement","core_stockreservation")
        with connection.cursor() as cursor:
            result={}
            for table in tables:
                cursor.execute(f"SELECT * FROM {connection.ops.quote_name(table)} ORDER BY id")
                result[table]=cursor.fetchall()
            return result

    def test_upgrade_and_reverse_preserve_history_and_do_not_release_existing_refusals(self):
        latest=MigrationExecutor(connection).loader.graph.leaf_nodes()
        previous=[("stock","0002_stockmovement_stock_movement_integer_quantity_and_more"),
                  ("backups","0022_recovery_sale_hold_identity")]
        try:
            MigrationExecutor(connection).migrate(previous)
            old=MigrationExecutor(connection).loader.project_state(previous).apps
            owner=old.get_model("core","User").objects.create(username="reconciliation-migration",is_active=False)
            shop=old.get_model("identity","Shop").objects.create(owner=owner,name="Archive")
            phone=old.get_model("identity","Installation").objects.create(user=owner,revoked_at=timezone.now())
            product=old.get_model("catalogue","Product").objects.create(shop=shop,name="Poids",unit_kind="weight",
                unit_label="kg",sale_price_fcfa=1000)
            event=old.get_model("core","Activity").objects.create(shop=shop,actor=owner,action="physical_loss",
                target_id=product.id,reason="Fait original",details={"response":{"kept":True}})
            movement=old.get_model("stock","StockMovement").objects.create(product=product,actor=owner,operation=event,
                kind="loss",quantity_milli=-500,unit_cost_micros=None,occurred_at=timezone.now(),reason="Perte conservée")
            refused=old.get_model("core","Activity").objects.create(shop=shop,actor=owner,action="refused",
                target_id=uuid.uuid4(),reason="Historique refusé",details={"response":{"state":"refused"}})
            reservation=old.get_model("stock","StockReservation").objects.create(product=product,operation=refused,
                quantity_milli=1000,state="rejected")
            # A preexisting immutable command can reference the new table after migration.
            command=old.get_model("core","Activity").objects.create(shop=shop,actor=owner,action="fixture_command",
                target_id=reservation.id,reason="Lien de qualification")
            before=self.snapshot()
            MigrationExecutor(connection).migrate(latest)
            self.assertEqual(self.snapshot(),before)
            from stock.models import StockReservationReconciliation
            from catalogue.models import Product
            from stock.services.stock_queries import with_stock
            self.assertEqual(StockReservationReconciliation.objects.count(),0)
            stock=with_stock(Product.objects.filter(id=product.id)).get()
            self.assertEqual((stock.stock_validated_milli,stock.stock_reserved_milli,stock.stock_available_milli),
                ("-500","1000","-1500"))
            StockReservationReconciliation.objects.create(reservation_id=reservation.id,movement_id=movement.id,
                operation_id=command.id,installation_id=phone.id,quantity_milli=500)
            stock=with_stock(Product.objects.filter(id=product.id)).get()
            self.assertEqual((stock.stock_validated_milli,stock.stock_reserved_milli,stock.stock_available_milli),
                ("-500","500","-1000"))
            self.assertEqual(self.snapshot(),before)
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA foreign_key_check")
                self.assertEqual(cursor.fetchall(),[])
            MigrationExecutor(connection).migrate(previous)
            self.assertEqual(self.snapshot(),before)
        finally:
            MigrationExecutor(connection).migrate(latest)
