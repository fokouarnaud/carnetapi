"""An additive closure table never turns a historical inventory into a closed recovery."""
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class RecoveryClosureMigrationTests(TransactionTestCase):
    def test_existing_inventory_capture_and_removed_identity_remain_unclosed_and_unchanged(self):
        executor=MigrationExecutor(connection);latest=executor.loader.graph.leaf_nodes()
        previous=[("backups","0027_recovery_collection_reviews")]
        try:
            executor.migrate(previous)
            old=executor.loader.project_state(previous).apps
            owner=old.get_model("core","User").objects.create(username="closure-migration-owner",is_active=False)
            shop=old.get_model("identity","Shop").objects.create(owner=owner,name="Archive")
            now=timezone.now()
            installation=old.get_model("identity","Installation").objects.create(user=owner,revoked_at=now)
            generation=old.get_model("backups","RestoreAccessGeneration").objects.create(operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),initiated_by=owner.id)
            registry=old.get_model("backups","RestoreSession").objects.create(generation=generation,inventory_recorded_at=now)
            device=old.get_model("backups","RecoveryDevice").objects.create(session=registry,shop=shop,
                origin_installation_id=installation.id,origin_user_id=owner.id,label="Téléphone déconnecté")
            deposit=old.get_model("backups","RecoveryJournalDeposit").objects.create(session=registry,shop=shop,
                operation_id=uuid.uuid4(),submitted_by=owner,submitted_installation=installation,reason="Copie reçue",
                format_version=1,content_sha256="a"*64,receipt={"capture_state":"captured"})
            capture=old.get_model("backups","RecoveryJournalOperation").objects.create(session=registry,shop=shop,
                first_deposit=deposit,operation_id=uuid.uuid4(),origin_user_id=owner.id,origin_installation_id=installation.id,
                operation_type="sale",occurred_at=now,origin_recorded_at=now,payload={"surviving":"fact"},dependencies=[],content_sha256="b"*64)
            executor=MigrationExecutor(connection);executor.migrate(latest)
            current=executor.loader.project_state(latest).apps
            self.assertEqual(current.get_model("backups","RecoveryClosure").objects.count(),0)
            self.assertEqual(current.get_model("backups","RecoveryDevice").objects.get(id=device.id).label,"Téléphone déconnecté")
            self.assertEqual(current.get_model("backups","RestoreSession").objects.get(id=registry.id).inventory_recorded_at,now)
            self.assertEqual(current.get_model("backups","RecoveryJournalOperation").objects.get(id=capture.id).payload,{"surviving":"fact"})
            self.assertFalse(current.get_model("core","User").objects.get(id=owner.id).is_active)
            self.assertEqual(current.get_model("identity","Installation").objects.get(id=installation.id).revoked_at,now)
        finally:MigrationExecutor(connection).migrate(latest)
