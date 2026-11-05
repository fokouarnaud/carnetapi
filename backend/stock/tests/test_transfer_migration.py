"""Additive transfer tables leave existing stock, unknown costs and access untouched."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class TransferMigrationTests(TransactionTestCase):
    def snapshot(self):
        with connection.cursor() as cursor:
            result={}
            for table in ("core_user","core_shop","core_installation","core_product","core_activity","core_stockmovement","core_stockreservation"):
                cursor.execute(f"SELECT * FROM {connection.ops.quote_name(table)} ORDER BY id")
                result[table]=cursor.fetchall()
            return result

    def test_upgrade_preserves_existing_uuid_costs_stock_reservations_and_withdrawn_rights(self):
        latest=MigrationExecutor(connection).loader.graph.leaf_nodes()
        previous=[node for node in latest if node[0]!="stock"]+[("stock","0003_reservation_reconciliation")]
        try:
            MigrationExecutor(connection).migrate(previous)
            old=MigrationExecutor(connection).loader.project_state(previous).apps
            owner=old.get_model("core","User").objects.create(username="transfer-archived-owner",is_active=False)
            shop=old.get_model("identity","Shop").objects.create(owner=owner,name="Archive")
            old.get_model("identity","Installation").objects.create(user=owner,revoked_at=timezone.now())
            product=old.get_model("catalogue","Product").objects.create(shop=shop,name="Historique",unit_kind="piece",unit_label="pièce",sale_price_fcfa=500)
            operation=old.get_model("core","Activity").objects.create(shop=shop,actor=owner,action="opening_stock",target_id=product.id,reason="Historique")
            old.get_model("stock","StockMovement").objects.create(product=product,actor=owner,operation=operation,
                kind="opening",quantity_milli=3000,unit_cost_micros=None,occurred_at=timezone.now(),reason="Historique")
            old.get_model("stock","StockReservation").objects.create(product=product,operation=operation,quantity_milli=1000,state="rejected")
            before=self.snapshot()
            MigrationExecutor(connection).migrate(latest)
            self.assertEqual(self.snapshot(),before)
            current=MigrationExecutor(connection).loader.project_state(latest).apps
            for name in ("StockTransfer","StockTransferLine","StockTransferReceipt","StockTransferReceiptLine"):
                self.assertEqual(current.get_model("stock",name).objects.count(),0)
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA foreign_key_check")
                self.assertEqual(cursor.fetchall(),[])
            MigrationExecutor(connection).migrate(previous)
            self.assertEqual(self.snapshot(),before)
        finally:MigrationExecutor(connection).migrate(latest)
