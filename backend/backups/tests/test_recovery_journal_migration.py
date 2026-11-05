"""0019 preserves archived observations and never invents a recovered journal."""
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class RecoveryJournalMigrationTests(TransactionTestCase):
    def test_existing_registry_and_rights_survive_without_fabricated_captures(self):
        executor=MigrationExecutor(connection)
        latest=executor.loader.graph.leaf_nodes()
        previous=[("backups","0018_restoration_recovery_inventory")]
        try:
            executor.migrate(previous)
            old=executor.loader.project_state(previous).apps
            owner=old.get_model("core","User").objects.create(username="journal-migration-owner",is_active=False)
            shop=old.get_model("identity","Shop").objects.create(owner=owner,name="Archive A")
            generation=old.get_model("backups","RestoreAccessGeneration").objects.create(
                operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),initiated_by=owner.id)
            session=old.get_model("backups","RestoreSession").objects.create(generation=generation,inventory_recorded_at=timezone.now())
            observed=old.get_model("backups","RecoveryDevice").objects.create(session=session,shop=shop,
                origin_installation_id=uuid.uuid4(),origin_user_id=uuid.uuid4(),label="Copie disponible")
            executor=MigrationExecutor(connection)
            executor.migrate(latest)
            current=executor.loader.project_state(latest).apps
            self.assertEqual(current.get_model("backups","RestoreSession").objects.get().id,session.id)
            phone=current.get_model("backups","RecoveryDevice").objects.get()
            self.assertEqual((phone.id,phone.origin_installation_id,phone.origin_user_id,phone.label),
                (observed.id,observed.origin_installation_id,observed.origin_user_id,"Copie disponible"))
            self.assertFalse(current.get_model("core","User").objects.get(id=owner.id).is_active)
            self.assertEqual(current.get_model("backups","RecoveryJournalDeposit").objects.count(),0)
            self.assertEqual(current.get_model("backups","RecoveryJournalOperation").objects.count(),0)
        finally:MigrationExecutor(connection).migrate(latest)
