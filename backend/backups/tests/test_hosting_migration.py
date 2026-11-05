"""Hosting additions preserve historical business data and reject fractional bytes."""
from datetime import datetime, timezone as utc
from django.db import connection, IntegrityError
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class HostingMigrationTests(TransactionTestCase):
    def snapshot(self):
        with connection.cursor() as cursor:
            result={}
            for table in ("core_user","core_shop","core_installation","core_product","core_activity","core_stockmovement"):
                cursor.execute(f"SELECT * FROM {connection.ops.quote_name(table)} ORDER BY id")
                result[table]=cursor.fetchall()
            return result

    def test_additive_hosting_tables_preserve_existing_stock_unknown_cost_and_revocations(self):
        latest=MigrationExecutor(connection).loader.graph.leaf_nodes()
        # Resolve the actual previous migration without assuming a filename suffix.
        previous=[node for node in latest if node[0]!="backups"]+[
            next(node.key for node in MigrationExecutor(connection).loader.graph.node_map[("backups","0023_hosting_expiration_reminders")].parents if node.key[0]=="backups")]
        try:
            MigrationExecutor(connection).migrate(previous)
            apps=MigrationExecutor(connection).loader.project_state(previous).apps
            user=apps.get_model("core","User").objects.create(username="hosting-historical",is_active=False)
            shop=apps.get_model("identity","Shop").objects.create(owner=user,name="Historique")
            apps.get_model("identity","Installation").objects.create(user=user,revoked_at=datetime(2026,10,1,tzinfo=utc.utc))
            product=apps.get_model("catalogue","Product").objects.create(shop=shop,name="Coût inconnu",unit_kind="piece",unit_label="pièce",sale_price_fcfa=500)
            event=apps.get_model("core","Activity").objects.create(shop=shop,actor=user,action="opening_stock",target_id=product.id,reason="Fixture privée")
            apps.get_model("stock","StockMovement").objects.create(product=product,actor=user,operation=event,
                kind="opening",quantity_milli=3000,unit_cost_micros=None,occurred_at=datetime(2026,10,1,tzinfo=utc.utc),reason="Historique")
            before=self.snapshot()
            MigrationExecutor(connection).migrate(latest)
            self.assertEqual(self.snapshot(),before)
            current=MigrationExecutor(connection).loader.project_state(latest).apps
            for name in ("HostingConfiguration","HostingConfigurationChange","HostingReminder","HostingReminderAttempt","HostingObservation"):
                self.assertEqual(current.get_model("backups",name).objects.count(),0)
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA foreign_key_check");self.assertEqual(cursor.fetchall(),[])
        finally:MigrationExecutor(connection).migrate(latest)

    def test_exact_byte_constraint_upgrade_refuses_fraction_without_rewriting_it(self):
        latest=MigrationExecutor(connection).loader.graph.leaf_nodes()
        previous=[node for node in latest if node[0]!="backups"]+[("backups","0023_hosting_expiration_reminders")]
        try:
            MigrationExecutor(connection).migrate(previous)
            apps=MigrationExecutor(connection).loader.project_state(previous).apps
            user=apps.get_model("core","User").objects.create(username="hosting-fraction")
            row=apps.get_model("backups","HostingObservation").objects.create(actor=user,operation_id=user.id,
                observed_at=datetime(2026,10,1,tzinfo=utc.utc),installed_bytes=123,quota_used_bytes=None,
                service_state="unknown",evidence_reference="Fixture",reason="Historique",request={},receipt={})
            with connection.cursor() as cursor:
                cursor.execute("UPDATE backups_hostingobservation SET installed_bytes=1.5 WHERE id=%s",[row.id.hex])
            with self.assertRaises(IntegrityError):MigrationExecutor(connection).migrate(latest)
            with connection.cursor() as cursor:
                cursor.execute("SELECT installed_bytes,quota_used_bytes FROM backups_hostingobservation WHERE id=%s",[row.id.hex])
                self.assertEqual(cursor.fetchone(),(1.5,None))
                cursor.execute("UPDATE backups_hostingobservation SET installed_bytes=123 WHERE id=%s",[row.id.hex])
            MigrationExecutor(connection).migrate(latest)
            with connection.cursor() as cursor:
                cursor.execute("SELECT installed_bytes,quota_used_bytes FROM backups_hostingobservation WHERE id=%s",[row.id.hex])
                self.assertEqual(cursor.fetchone(),(123,None))
        finally:MigrationExecutor(connection).migrate(latest)
