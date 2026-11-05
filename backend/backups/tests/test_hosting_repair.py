"""Interrupted-reminder review preserves evidence across rollback and later retries."""
from datetime import timedelta
import importlib.util
from pathlib import Path
import uuid
from unittest.mock import patch
from django.db import connection, OperationalError, IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from backups.models import HostingConfiguration, HostingReminder, HostingReminderAttempt, HostingReminderRepair
from backups.services.hosting_repair import _repair_under_control, HostingRepairError
from backups.tests import test_restore_access_console_input as input_fixtures
from identity.models import User
from html import unescape
from django.urls import reverse


class HostingRepairInputTests(input_fixtures.RestoreAccessConsoleInputTests):
    def setUp(self):
        path=Path(__file__).resolve().parents[3]/"tools/repair_hosting_reminder.py"
        spec=importlib.util.spec_from_file_location("hosting_console_input",path)
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)


class HostingRepairAtomicTests(TestCase):
    def setUp(self):
        self.actor=User.objects.create_superuser(username="repair-atomic",password=None)
        self.config=HostingConfiguration.objects.create(expires_at=timezone.now()+timedelta(days=7),recipient="operator@example.test",
            language="fr",evidence_reference="Fixture",updated_by=self.actor)
        self.reminder=HostingReminder.objects.create(expires_at=self.config.expires_at,lead_days=7)
        self.attempt=HostingReminderAttempt.objects.create(reminder=self.reminder,number=1,actor=self.actor,
            configuration_version=1,recipient="operator@example.test",sender="sender@example.test",language="fr",
            message_id="<interrupted@example.test>",subject="Original",body="Original",started_at=timezone.now(),request={})
        self.reminder.state="sending";self.reminder.version=1;self.reminder.current_attempt=self.attempt;self.reminder.save()
        self.operation=uuid.uuid4()

    def repair(self,**values):
        # Unit qualification of the transaction helper. Actual exclusive leases
        # and ORM binding are qualified in the separate console process tests.
        return _repair_under_control(self.actor,attempt_id=self.attempt.id,operation_id=self.operation,
            reason="Arrêt du processus, résultat inconnu",control={"mode":"open","version":1},**values)

    def test_failure_after_audit_and_attempt_update_rolls_back_every_state_and_receipt(self):
        def fail_reminder_update(execute,sql,params,many,context):
            if sql.startswith('UPDATE "backups_hostingreminder"'):raise OperationalError("private injected SQL failure")
            return execute(sql,params,many,context)
        with connection.execute_wrapper(fail_reminder_update),self.assertRaises(OperationalError):self.repair()
        self.assertEqual(HostingReminderRepair.objects.count(),0)
        self.attempt.refresh_from_db();self.reminder.refresh_from_db()
        self.assertEqual((self.attempt.state,self.reminder.state),("sending","sending"))
        self.assertIsNone(self.attempt.finished_at)
        self.assertEqual(self.attempt.failure_code,"")
        self.assertEqual(self.attempt.body,"Original")

    def test_old_receipt_replay_does_not_modify_a_later_attempt(self):
        receipt=self.repair()
        next_attempt=HostingReminderAttempt.objects.create(reminder=self.reminder,number=2,actor=self.actor,
            configuration_version=1,recipient="operator@example.test",sender="sender@example.test",language="fr",
            message_id="<interrupted@example.test>",subject="Original",body="Original",started_at=timezone.now(),request={})
        self.reminder.state="sending";self.reminder.version=2;self.reminder.current_attempt=next_attempt;self.reminder.save()
        self.assertEqual(self.repair(),receipt)
        self.operation=uuid.uuid4()
        with self.assertRaises(HostingRepairError):self.repair()
        self.reminder.refresh_from_db();next_attempt.refresh_from_db()
        self.assertEqual((self.reminder.version,self.reminder.state,next_attempt.state),(2,"sending","sending"))
        self.assertEqual(HostingReminderRepair.objects.count(),1)

    def test_clock_before_claim_cannot_invent_a_finish_date(self):
        with patch("django.utils.timezone.now",return_value=self.attempt.started_at-timedelta(seconds=1)):
            with self.assertRaisesRegex(HostingRepairError,"hosting_repair_clock_inconsistent"):self.repair()
        self.assertEqual(HostingReminderRepair.objects.count(),0)
        self.attempt.refresh_from_db();self.assertEqual(self.attempt.state,"sending")

    def test_audit_is_technical_readonly_and_localized_in_unfold(self):
        self.repair()
        url=reverse("admin:backups_hostingreminderrepair_changelist")
        self.client.force_login(self.actor)
        for language,label in (("fr","Examens des rappels d'hébergement interrompus"),("en","Hosting reminder interruption reviews")):
            with self.subTest(language=language):
                response=self.client.get(url,HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(response.status_code,200)
                self.assertIn(label,unescape(response.content.decode()))
                self.assertContains(response,"Arrêt du processus, résultat inconnu")
        self.assertEqual(self.client.get(reverse("admin:backups_hostingreminderrepair_add")).status_code,403)
        audit=HostingReminderRepair.objects.get()
        self.assertEqual(self.client.post(reverse("admin:backups_hostingreminderrepair_change",args=[audit.id]),{"reason":"Altéré"}).status_code,403)
        self.assertEqual(self.client.post(reverse("admin:backups_hostingreminderrepair_delete",args=[audit.id])).status_code,403)
        User.objects.filter(id=self.actor.id).update(is_superuser=False)
        self.assertEqual(self.client.get(url).status_code,403)
        audit.refresh_from_db();self.assertEqual(audit.reason,"Arrêt du processus, résultat inconnu")

    def test_sql_rejects_fractional_gate_versions_and_nonobject_audit_payloads(self):
        self.repair();audit=HostingReminderRepair.objects.get()
        for column,value in (("gate_version",1.5),("gate_version",0),("payload","[]"),("receipt","[]")):
            with self.subTest(column=column,value=value),self.assertRaises(IntegrityError),transaction.atomic():
                with connection.cursor() as cursor:cursor.execute(f"UPDATE backups_hostingreminderrepair SET {column}=%s WHERE id=%s",[value,audit.id.hex])
        audit.refresh_from_db();self.assertEqual(audit.gate_version,1)
