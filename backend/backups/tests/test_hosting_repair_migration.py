"""Adding interruption reviews never rewrites an unresolved SMTP claim."""
from datetime import datetime, timezone as utc
import uuid
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class HostingRepairMigrationTests(TransactionTestCase):
    def snapshot(self):
        with connection.cursor() as cursor:
            result={}
            for table in ("core_user","backups_hostingconfiguration","backups_hostingreminder","backups_hostingreminderattempt"):
                cursor.execute(f"SELECT * FROM {connection.ops.quote_name(table)} ORDER BY id");result[table]=cursor.fetchall()
            return result

    def test_forward_and_reverse_keep_original_sending_claim_actor_and_message_exactly(self):
        latest=MigrationExecutor(connection).loader.graph.leaf_nodes()
        previous=[node for node in latest if node[0]!="backups"]+[("backups","0024_hosting_exact_observations")]
        try:
            MigrationExecutor(connection).migrate(previous)
            apps=MigrationExecutor(connection).loader.project_state(previous).apps
            actor=apps.get_model("core","User").objects.create(username="interrupted-archived",is_active=False)
            config=apps.get_model("backups","HostingConfiguration").objects.create(expires_at=datetime(2026,11,10,tzinfo=utc.utc),
                recipient="operator@example.test",language="fr",evidence_reference="Historique",updated_by=actor)
            reminder=apps.get_model("backups","HostingReminder").objects.create(expires_at=config.expires_at,lead_days=7)
            attempt=apps.get_model("backups","HostingReminderAttempt").objects.create(reminder=reminder,number=1,actor=actor,
                configuration_version=1,command_id=uuid.uuid4(),recipient=config.recipient,sender="sender@example.test",language="fr",
                message_id="<archive@example.test>",subject="Historique",body="Contenu historique",request={"origin":"archive"},
                started_at=datetime(2026,11,3,tzinfo=utc.utc))
            reminder.state="sending";reminder.version=1;reminder.current_attempt=attempt;reminder.save()
            before=self.snapshot()
            MigrationExecutor(connection).migrate(latest)
            self.assertEqual(self.snapshot(),before)
            current=MigrationExecutor(connection).loader.project_state(latest).apps
            self.assertEqual(current.get_model("backups","HostingReminderRepair").objects.count(),0)
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA foreign_key_check");self.assertEqual(cursor.fetchall(),[])
            MigrationExecutor(connection).migrate(previous)
            self.assertEqual(self.snapshot(),before)
        finally:MigrationExecutor(connection).migrate(latest)
