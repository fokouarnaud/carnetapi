"""An old restore keeps identities and explicitly lacks a recorded inventory."""
import uuid
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class RecoverySessionMigrationTests(TransactionTestCase):
    def test_existing_generation_is_preserved_without_inventing_a_device_inventory(self):
        executor=MigrationExecutor(connection)
        latest=executor.loader.graph.leaf_nodes()
        previous=[("backups","0017_restore_access_reviews")]
        try:
            executor.migrate(previous)
            old=executor.loader.project_state(previous).apps
            owner=old.get_model("core","User").objects.create(username="legacy-recovery-owner")
            shop=old.get_model("identity","Shop").objects.create(owner=owner,name="Archive existante")
            phone=old.get_model("identity","Installation").objects.create(user=owner,label="Ancien téléphone")
            generation=old.get_model("backups","RestoreAccessGeneration").objects.create(
                operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),initiated_by=owner.id)
            executor=MigrationExecutor(connection)
            executor.migrate(latest)
            current=executor.loader.project_state(latest).apps
            restored=current.get_model("backups","RestoreAccessGeneration").objects.get(id=generation.id)
            self.assertEqual(restored.operation_id,generation.operation_id)
            self.assertEqual(restored.backup_id,generation.backup_id)
            self.assertEqual(restored.current_slot,1)
            self.assertTrue(current.get_model("core","User").objects.get(id=owner.id).is_active)
            self.assertEqual(current.get_model("identity","Installation").objects.get(id=phone.id).label,"Ancien téléphone")
            self.assertEqual(current.get_model("backups","RestoreSession").objects.count(),0)
            from identity.models import Shop
            from backups.services.recovery_status import recovery_status_for_shop
            status=recovery_status_for_shop(Shop.objects.get(id=shop.id))
            self.assertEqual(status["state"],"recovering")
            self.assertIsNone(status["known_devices"])
            self.assertIsNone(status["snapshot_at"])
        finally:
            MigrationExecutor(connection).migrate(latest)
