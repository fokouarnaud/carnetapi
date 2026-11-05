"""0020 preserves captured evidence without inventing integration or access."""
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class RecoveryIntegrationMigrationTests(TransactionTestCase):
    def test_captured_source_and_inactive_identity_survive_without_business_replay(self):
        executor=MigrationExecutor(connection)
        latest=executor.loader.graph.leaf_nodes()
        previous=[("backups","0019_recovery_journal_capture")]
        try:
            executor.migrate(previous)
            old=executor.loader.project_state(previous).apps
            owner=old.get_model("core","User").objects.create(username="integration-migration-owner",is_active=False)
            shop=old.get_model("identity","Shop").objects.create(owner=owner,name="Archive A")
            install=old.get_model("identity","Installation").objects.create(user=owner,revoked_at=timezone.now())
            generation=old.get_model("backups","RestoreAccessGeneration").objects.create(
                operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),initiated_by=owner.id)
            session=old.get_model("backups","RestoreSession").objects.create(generation=generation)
            deposit=old.get_model("backups","RecoveryJournalDeposit").objects.create(session=session,shop=shop,
                operation_id=uuid.uuid4(),submitted_by=owner,submitted_installation=install,reason="Copie survivante",
                format_version=1,content_sha256="a"*64,receipt={"capture_state":"captured"})
            capture=old.get_model("backups","RecoveryJournalOperation").objects.create(session=session,shop=shop,
                first_deposit=deposit,operation_id=uuid.uuid4(),origin_user_id=uuid.uuid4(),origin_installation_id=uuid.uuid4(),
                operation_type="sale",occurred_at=timezone.now(),origin_recorded_at=timezone.now(),
                payload={"original_fact":500},dependencies=[],content_sha256="b"*64)
            executor=MigrationExecutor(connection)
            executor.migrate(latest)
            current=executor.loader.project_state(latest).apps
            stored=current.get_model("backups","RecoveryJournalOperation").objects.get(id=capture.id)
            self.assertEqual((stored.operation_id,stored.origin_user_id,stored.origin_installation_id,stored.payload,stored.content_sha256),
                (capture.operation_id,capture.origin_user_id,capture.origin_installation_id,{"original_fact":500},"b"*64))
            self.assertEqual(current.get_model("backups","RecoveryJournalDeposit").objects.get().receipt,{"capture_state":"captured"})
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN SELECT id FROM backups_recoveryjournaloperation WHERE operation_id=%s",[capture.operation_id.hex])
                plan=" ".join(str(row) for row in cursor.fetchall())
            self.assertIn("recovery_operation_uuid",plan)
            self.assertFalse(current.get_model("core","User").objects.get(id=owner.id).is_active)
            self.assertIsNotNone(current.get_model("identity","Installation").objects.get(id=install.id).revoked_at)
            for app,name in (("backups","RecoveredOperationOrigin"),("backups","RecoveryIntegrationDecision"),
                             ("core","Activity"),("sales","Sale"),("cash","CashMovement"),("stock","StockMovement")):
                self.assertEqual(current.get_model(app,name).objects.count(),0,name)
        finally:MigrationExecutor(connection).migrate(latest)
