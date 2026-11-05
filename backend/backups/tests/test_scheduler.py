"""Scoped external trigger: real daily snapshots, bounded sends and visible gaps."""
from datetime import datetime,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import uuid
from cryptography.fernet import Fernet
from django.test import TransactionTestCase,override_settings
from rest_framework.test import APIClient
from backups.models import BackupJob,BackupDispatch,SendAttempt
from identity.models import User


class BackupSchedulerTests(TransactionTestCase):
    def setUp(self):
        self.directory=TemporaryDirectory(prefix="backup-scheduler-")
        self.addCleanup(self.directory.cleanup)
        self.operator=User.objects.create_superuser(username="scheduler-technical",password=None)
        self.owner=User.objects.create_user(username="scheduler-owner")
        self.token="test-only-scheduler-token-which-is-dedicated-0001"
        self.options=override_settings(BACKUP_ROOT=Path(self.directory.name),BACKUP_ENCRYPTION_KEY=Fernet.generate_key(),
            BACKUP_SCHEDULER_TOKEN=self.token,BACKUP_SCHEDULER_ACTOR_ID=str(self.operator.id),EMAIL_HOST_USER="sender@example.test")
        self.options.enable()
        self.addCleanup(self.options.disable)
        self.api=APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.url="/api/v1/backups/trigger/"
        self.submitted=[]

    def tick(self,at):
        def accepted(message):
            self.submitted.append(message)
        with patch("django.utils.timezone.now",return_value=at),patch("backups.services.sending.submit_smtp",side_effect=accepted):
            return self.api.post(self.url,{},format="json")

    def test_wrong_secret_and_body_parameters_cannot_read_data_or_execute_a_snapshot(self):
        self.api.credentials(HTTP_AUTHORIZATION="Bearer wrong")
        self.assertEqual(self.tick(datetime(2026,10,19,1,tzinfo=timezone.utc)).status_code,401)
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(self.api.get("/api/v1/backups/").status_code,401)
        self.assertEqual(self.api.post(self.url,{"recipient":"other@example.test"},format="json").status_code,400)
        self.assertFalse(BackupJob.objects.exists())

    def test_openapi_matches_real_trigger_results_errors_and_global_maintenance(self):
        import json
        from drf_spectacular.generators import SchemaGenerator
        from jsonschema import validate, ValidationError as SchemaValidationError
        from django.test import RequestFactory
        from backups.middleware import RestoreGateMiddleware
        from backups.models import BackupConfiguration
        schema=SchemaGenerator().get_schema(request=None,public=True)
        operation=schema["paths"][self.url]["post"]
        self.assertTrue({"200","400","401","503"}.issubset(operation["responses"]))
        request_schema=operation["requestBody"]["content"]["application/json"]["schema"]
        validate({},request_schema)
        with self.assertRaises(SchemaValidationError):
            validate({"recipient":"forbidden@example.test"},request_schema)
        BackupConfiguration.objects.create(singleton=1,enabled=False)
        responses=[self.api.post(self.url,{},format="json")]
        self.assertEqual(responses[-1].data,{"status":"disabled"})
        User.objects.filter(id=self.operator.id).update(is_active=False)
        responses.append(self.api.post(self.url,{},format="json"))
        self.assertEqual(responses[-1].status_code,503)
        self.assertEqual(responses[-1].data,{"code":"backup_scheduler_unavailable"})
        self.api.credentials(HTTP_AUTHORIZATION="Bearer invalid")
        responses.append(self.api.post(self.url,{},format="json"))
        self.assertEqual(responses[-1].status_code,401)
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {self.token}")
        responses.append(self.api.post(self.url,{"recipient":"forbidden@example.test"},format="json"))
        self.assertEqual(responses[-1].status_code,400)
        with override_settings(RESTORE_GATE_PATH=Path(self.directory.name)/"missing-gate.sqlite3"):
            responses.append(RestoreGateMiddleware(lambda request:self.fail("Maintenance must stop the handler"))(
                RequestFactory().post(self.url)))
        self.assertEqual(responses[-1].status_code,503)
        for response in responses:
            with self.subTest(status=response.status_code,body=json.loads(response.content)):
                documented=operation["responses"][str(response.status_code)]["content"]["application/json"]["schema"]
                validate(json.loads(response.content),{**documented,"components":schema["components"]})
        self.assertFalse(BackupJob.objects.exists())

    def test_daily_capture_is_due_at_one_utc_and_next_call_sends_without_duplicate_snapshot(self):
        early=self.tick(datetime(2026,10,19,0,59,tzinfo=timezone.utc))
        self.assertEqual(early.status_code,200)
        self.assertEqual(early.data,{"status":"idle"})
        self.assertFalse(BackupJob.objects.exists())
        prepared=self.tick(datetime(2026,10,19,1,tzinfo=timezone.utc))
        self.assertEqual(prepared.status_code,200)
        self.assertEqual(prepared.data,{"status":"prepared"})
        job=BackupJob.objects.get()
        self.assertEqual(job.recipient,"sender@example.test")
        self.assertEqual(job.state,"ready")
        self.assertFalse(self.submitted)
        sent=self.tick(datetime(2026,10,19,1,1,tzinfo=timezone.utc))
        self.assertEqual(sent.status_code,200)
        self.assertEqual(sent.data,{"status":"accepted"})
        self.assertEqual(len(self.submitted),1)
        self.assertEqual(self.tick(datetime(2026,10,19,1,2,tzinfo=timezone.utc)).data,{"status":"idle"})
        self.assertEqual(BackupJob.objects.count(),1)
        self.assertEqual(BackupDispatch.objects.get().state,"accepted")
        job.refresh_from_db()
        self.assertEqual(job.cleanup_state,"cleaned")

    def test_resume_after_missing_days_creates_one_current_capture_and_journals_gap(self):
        self.assertEqual(self.tick(datetime(2026,10,19,1,tzinfo=timezone.utc)).status_code,200)
        self.assertEqual(self.tick(datetime(2026,10,19,1,1,tzinfo=timezone.utc)).status_code,200)
        resumed=self.tick(datetime(2026,10,22,9,tzinfo=timezone.utc))
        self.assertEqual(resumed.status_code,200)
        self.assertEqual(resumed.data,{"status":"prepared"})
        self.assertEqual(BackupJob.objects.count(),2)
        from django.apps import apps
        run=apps.get_model("backups","BackupScheduleRun").objects.order_by("-sequence").first()
        self.assertEqual(str(run.local_date),"2026-10-22")
        self.assertEqual(run.missed_day_count,2)
        self.assertEqual(str(run.missed_from),"2026-10-20")
        self.assertEqual(str(run.missed_to),"2026-10-21")
        self.assertEqual(run.scheduled_for,datetime(2026,10,22,1,tzinfo=timezone.utc))
        self.assertEqual(run.job.created_at,datetime(2026,10,22,9,tzinfo=timezone.utc))

    def test_any_nonabandoned_incomplete_backup_blocks_new_automatic_capture(self):
        manual=BackupJob.objects.create(operation_id=uuid.uuid4(),requested_by=self.operator,
            reason="ancienne capture échouée",recipient="sender@example.test",key_id="primary",state="failed",failure_code="snapshot_failed")
        blocked=self.tick(datetime(2026,10,19,1,tzinfo=timezone.utc))
        self.assertEqual(blocked.status_code,200)
        self.assertEqual(blocked.data,{"status":"blocked"})
        self.assertEqual(BackupJob.objects.count(),1)
        from backups.services.abandonment import abandon_backup
        abandon_backup(self.operator,manual.id,uuid.uuid4(),"Autoriser une nouvelle capture")
        self.assertEqual(self.tick(datetime(2026,10,19,1,1,tzinfo=timezone.utc)).data,{"status":"prepared"})
        self.assertEqual(BackupJob.objects.count(),2)

    def test_disabled_schedule_and_removed_actor_do_not_generate_or_send(self):
        from backups.services.configuration import change_configuration
        change_configuration(self.operator,uuid.uuid4(),0,False,2,0,"Désactivation")
        self.assertEqual(self.tick(datetime(2026,10,19,1,tzinfo=timezone.utc)).data,{"status":"disabled"})
        with override_settings(BACKUP_SCHEDULER_ACTOR_ID=str(self.owner.id)):
            self.assertEqual(self.tick(datetime(2026,10,19,1,tzinfo=timezone.utc)).status_code,503)
        self.assertFalse(BackupJob.objects.exists())
        self.assertFalse(SendAttempt.objects.exists())

    def test_trigger_configuration_requires_dedicated_secret(self):
        from django.conf import settings
        with override_settings(BACKUP_SCHEDULER_TOKEN=settings.SECRET_KEY):
            self.assertEqual(self.tick(datetime(2026,10,19,1,tzinfo=timezone.utc)).status_code,503)
        self.assertFalse(BackupJob.objects.exists())

    def test_schedule_storage_rejects_duplicate_singletons_and_invalid_gap(self):
        from django.apps import apps
        from django.db import IntegrityError,transaction
        state=apps.get_model("backups","BackupSchedulerState")
        run=apps.get_model("backups","BackupScheduleRun")
        state.objects.create()
        with self.assertRaises(IntegrityError),transaction.atomic():
            state.objects.create()
        with self.assertRaises(IntegrityError),transaction.atomic():
            run.objects.create(sequence=1,local_date="2026-10-22",scheduled_for=datetime(2026,10,22,1,tzinfo=timezone.utc),
                actor=self.operator,recipient="sender@example.test",missed_day_count=2)

    def test_missing_key_or_invalid_sender_creates_no_run_or_job(self):
        from backups.models import BackupScheduleRun
        for options in ({"BACKUP_ENCRYPTION_KEY":""},{"EMAIL_HOST_USER":""},
                {"EMAIL_HOST_USER":"sender@example.test\r\nBcc: outsider@example.test"}):
            with self.subTest(options=tuple(options)),override_settings(**options):
                self.assertEqual(self.tick(datetime(2026,10,19,1,tzinfo=timezone.utc)).status_code,503)
                self.assertFalse(BackupScheduleRun.objects.exists())
                self.assertFalse(BackupJob.objects.exists())

    def test_get_cannot_execute_and_https_is_required_in_production(self):
        from backups.models import BackupSchedulerState
        self.assertEqual(self.api.get(self.url).status_code,405)
        with override_settings(APP_ENV="production",SECURE_SSL_REDIRECT=False):
            self.assertEqual(self.api.post(self.url,{},format="json").status_code,503)
        self.assertFalse(BackupSchedulerState.objects.exists())

    def test_removed_actor_blocks_pending_send_and_new_token_resumes_original_archive(self):
        self.assertEqual(self.tick(datetime(2026,10,19,1,tzinfo=timezone.utc)).data,{"status":"prepared"})
        User.objects.filter(id=self.operator.id).update(is_superuser=False)
        self.assertEqual(self.tick(datetime(2026,10,19,1,1,tzinfo=timezone.utc)).status_code,503)
        self.assertFalse(SendAttempt.objects.exists())
        User.objects.filter(id=self.operator.id).update(is_superuser=True)
        replacement="replacement-trigger-secret-which-is-also-dedicated-0002"
        with override_settings(BACKUP_SCHEDULER_TOKEN=replacement):
            self.assertEqual(self.tick(datetime(2026,10,19,1,2,tzinfo=timezone.utc)).status_code,401)
            self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {replacement}")
            self.assertEqual(self.tick(datetime(2026,10,19,1,3,tzinfo=timezone.utc)).data,{"status":"accepted"})
        self.assertEqual(BackupJob.objects.count(),1)

    def test_failed_smtp_retries_at_fifteen_minutes_three_times_then_blocks_new_capture(self):
        import smtplib
        self.tick(datetime(2026,10,19,1,tzinfo=timezone.utc))
        def rejected(at):
            with patch("django.utils.timezone.now",return_value=at),patch("backups.services.sending.submit_smtp",
                    side_effect=smtplib.SMTPDataError(550,b"test-only rejection")):
                return self.api.post(self.url,{},format="json")
        self.assertEqual(rejected(datetime(2026,10,19,1,1,tzinfo=timezone.utc)).status_code,200)
        self.tick(datetime(2026,10,19,1,15,tzinfo=timezone.utc))
        self.assertEqual(SendAttempt.objects.count(),1)
        for minute in (16,31,46):
            self.assertEqual(rejected(datetime(2026,10,19,1,minute,tzinfo=timezone.utc)).status_code,200)
        self.assertEqual(SendAttempt.objects.count(),4)
        self.assertEqual(list(SendAttempt.objects.order_by("number").values_list("retry_number",flat=True)),[0,1,2,3])
        self.assertEqual(self.tick(datetime(2026,10,22,9,tzinfo=timezone.utc)).data,{"status":"incomplete"})
        self.assertEqual(BackupJob.objects.count(),1)
        self.assertFalse(self.submitted)

    def test_old_claim_without_snapshot_is_journalled_as_missed_then_captures_current_day(self):
        from backups.models import BackupScheduleRun
        claimed=BackupScheduleRun.objects.create(sequence=1,local_date="2026-10-19",
            scheduled_for=datetime(2026,10,19,1,tzinfo=timezone.utc),actor=self.operator,recipient="sender@example.test")
        self.assertEqual(self.tick(datetime(2026,10,20,9,tzinfo=timezone.utc)).data,{"status":"prepared"})
        latest=BackupScheduleRun.objects.latest("sequence")
        self.assertEqual((latest.missed_day_count,str(latest.missed_from),str(latest.missed_to)),(1,"2026-10-19","2026-10-19"))
        claimed.refresh_from_db()
        self.assertEqual(claimed.failure_code,"scheduler_capture_not_started")
        self.assertEqual(BackupJob.objects.count(),1)

    def test_interrupted_snapshot_requires_explicit_abandonment_before_new_capture(self):
        from backups.models import BackupScheduleRun
        run=BackupScheduleRun.objects.create(sequence=1,local_date="2026-10-19",
            scheduled_for=datetime(2026,10,19,1,tzinfo=timezone.utc),actor=self.operator,recipient="sender@example.test")
        job=BackupJob.objects.create(operation_id=uuid.uuid5(run.id,"prepare"),requested_by=self.operator,
            reason="capture interrompue",recipient="sender@example.test",key_id="primary")
        self.assertEqual(self.tick(datetime(2026,10,20,9,tzinfo=timezone.utc)).data,{"status":"blocked"})
        job.refresh_from_db()
        self.assertEqual((job.state,job.failure_code),("failed","scheduler_preparation_interrupted"))
        from backups.services.abandonment import abandon_backup
        abandon_backup(self.operator,job.id,uuid.uuid4(),"Interruption vérifiée")
        self.assertEqual(self.tick(datetime(2026,10,20,9,1,tzinfo=timezone.utc)).data,{"status":"prepared"})
        latest=BackupScheduleRun.objects.latest("sequence")
        self.assertEqual((latest.missed_day_count,str(latest.missed_from),str(latest.missed_to)),
            (1,"2026-10-19","2026-10-19"))

    def test_run_history_is_technical_readonly_and_translated_in_unfold(self):
        from django.test import Client
        from django.urls import reverse
        self.tick(datetime(2026,10,19,1,tzinfo=timezone.utc))
        client=Client()
        client.force_login(self.operator)
        for language,label in (("en","Daily backup runs"),("fr","Exécutions quotidiennes des sauvegardes")):
            with self.subTest(language=language):
                response=client.get(reverse("admin:backups_backupschedulerun_changelist"),HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(response.status_code,200)
                self.assertContains(response,label)
                self.assertNotContains(response,self.token)
        self.assertEqual(client.get(reverse("admin:backups_backupschedulerun_add")).status_code,403)
        User.objects.filter(id=self.owner.id).update(is_staff=True)
        client.force_login(self.owner)
        self.assertEqual(client.get(reverse("admin:backups_backupschedulerun_changelist")).status_code,403)
