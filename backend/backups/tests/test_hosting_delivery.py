"""Failures, retries, current roles and restoration do not invent email delivery."""
from datetime import datetime, timezone as utc
import uuid
from unittest.mock import patch
from django.core import mail
from django.test import TestCase, override_settings
from django.core.management import call_command
from io import StringIO
from backups.tests import test_hosting as fixtures
from backups.models import HostingReminder, HostingReminderAttempt, HostingConfigurationChange, RestoreAccessGeneration
from identity.models import User


class HostingDeliveryTests(TestCase):
    setUp=fixtures.HostingApiTests.setUp
    configure=fixtures.HostingApiTests.configure
    trigger=fixtures.HostingApiTests.trigger

    def prepare(self,**values):
        with patch("django.utils.timezone.now",return_value=datetime(2026,11,1,tzinfo=utc.utc)):
            response,_=self.configure(**values)
        self.assertEqual(response.status_code,200,response.content)

    def test_manual_retry_acknowledges_possible_duplicate_and_reuses_the_command_receipt(self):
        self.prepare()
        at=datetime(2026,11,3,tzinfo=utc.utc)
        with patch("django.core.mail.message.EmailMessage.send",side_effect=TimeoutError("private")):
            self.assertEqual(self.trigger(at).data["status"],"uncertain")
        reminder=HostingReminder.objects.get(lead_days=7)
        command={"operation_id":str(uuid.uuid4()),"expected_attempt_id":str(reminder.current_attempt_id),
            "possible_duplicate_confirmed":False,"reason":"Reprise manuelle, doublon possible connu"}
        path=f"/api/v1/hosting/reminders/{reminder.id}/retry/"
        with patch("django.utils.timezone.now",return_value=at):
            self.assertEqual(self.client.post(path,command,format="json").status_code,409)
            command["possible_duplicate_confirmed"]=True
            response=self.client.post(path,command,format="json")
            self.assertEqual(response.status_code,201,response.content)
            self.assertEqual(response.data["state"],"accepted")
            replay=self.client.post(path,command,format="json")
            self.assertEqual((replay.status_code,replay.data),(200,response.data))
        self.assertEqual(len(mail.outbox),1)
        self.assertEqual(HostingReminderAttempt.objects.count(),2)
        history=self.client.get(f"/api/v1/hosting/reminders/{reminder.id}/attempts/")
        self.assertEqual([row["state"] for row in history.data["results"]],["uncertain","accepted"])
        self.assertEqual(history.data["results"][0]["message_id"],history.data["results"][1]["message_id"])

    def test_changing_recipient_language_or_disabling_does_not_send_or_reset_an_accepted_reminder(self):
        self.prepare()
        at=datetime(2026,11,3,tzinfo=utc.utc)
        self.assertEqual(self.trigger(at).data["status"],"accepted")
        with patch("django.utils.timezone.now",return_value=at):
            changed,_=self.configure(expected_version=1,recipient="new@example.test",language="en")
        self.assertEqual(changed.status_code,200,changed.content)
        self.assertEqual(self.trigger(at).data["status"],"idle")
        self.assertEqual(len(mail.outbox),1)
        self.assertEqual(HostingConfigurationChange.objects.count(),2)
        self.assertEqual(self.trigger(datetime(2026,11,8,tzinfo=utc.utc)).data["status"],"accepted")
        self.assertEqual(mail.outbox[-1].to,["new@example.test"])
        self.assertIn("reported expiry",mail.outbox[-1].subject)

    def test_new_restore_generation_blocks_archived_configuration_until_current_confirmation(self):
        self.prepare()
        generation=RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),initiated_by=self.operator.id)
        at=datetime(2026,11,3,tzinfo=utc.utc)
        self.assertEqual(self.trigger(at).data["status"],"awaiting_verification")
        self.assertEqual(len(getattr(mail,"outbox",[])),0)
        with patch("django.utils.timezone.now",return_value=at):
            confirmed,_=self.configure(expected_version=1)
        self.assertEqual(confirmed.status_code,200,confirmed.content)
        self.assertEqual(confirmed.data["verified_generation_id"],str(generation.id))
        self.assertEqual(self.trigger(at).data["status"],"accepted")

    def test_technical_role_is_current_and_token_is_separate_from_other_secrets(self):
        self.prepare()
        at=datetime(2026,11,3,tzinfo=utc.utc)
        User.objects.filter(id=self.operator.id).update(is_superuser=False)
        self.assertEqual(self.trigger(at).status_code,503)
        self.assertEqual(self.client.get("/api/v1/hosting/configuration/").status_code,403)
        self.assertEqual(HostingReminderAttempt.objects.count(),0)
        User.objects.filter(id=self.operator.id).update(is_superuser=True)
        for values in ({"HOSTING_REMINDER_TOKEN":"short"},{"BACKUP_SCHEDULER_TOKEN":"h"*48},
                       {"EMAIL_HOST_PASSWORD":"h"*48},{"APP_ENV":"production","SECURE_SSL_REDIRECT":False}):
            with self.subTest(values=list(values)),override_settings(**values):
                self.assertEqual(self.trigger(at).status_code,503)
        self.assertEqual(HostingReminderAttempt.objects.count(),0)

    def test_missing_windows_are_visible_and_trigger_body_cannot_choose_a_recipient_or_time(self):
        self.prepare()
        from rest_framework.test import APIClient
        api=APIClient();api.credentials(HTTP_AUTHORIZATION="Bearer "+"h"*48)
        self.assertEqual(api.post("/api/v1/hosting/trigger/",{"recipient":"other@example.test"},format="json").status_code,400)
        at=datetime(2026,11,5,tzinfo=utc.utc)
        self.assertEqual(self.trigger(at).data["status"],"idle")
        with patch("django.utils.timezone.now",return_value=at):
            state=self.client.get("/api/v1/hosting/status/")
        self.assertEqual(state.data["reminders"][0]["effective_state"],"missed")
        self.assertIsNone(state.data["backup"]["last_accepted_at"])
        self.assertEqual(HostingReminderAttempt.objects.count(),0)

    def test_console_uses_the_same_bounded_step_and_writes_only_a_minimal_status(self):
        self.prepare()
        output=StringIO()
        with patch("django.utils.timezone.now",return_value=datetime(2026,11,3,tzinfo=utc.utc)):
            call_command("hosting_reminder_step",stdout=output)
        self.assertEqual(output.getvalue().strip(),'{"status": "accepted"}')
        self.assertEqual(len(mail.outbox),1)

    def test_an_exhausted_reported_quota_requires_attention_before_expiration(self):
        self.prepare(expires_at="2026-12-10T15:00:00Z")
        at=datetime(2026,11,1,tzinfo=utc.utc)
        with patch("django.utils.timezone.now",return_value=at):
            response=self.client.post("/api/v1/hosting/observations/",{
                "id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"observed_at":"2026-10-20T10:00:00Z",
                "quota_used_bytes":500,"quota_limit_bytes":500,"service_state":"up",
                "evidence_reference":"Quota constaté","reason":"Espace épuisé"},format="json")
            self.assertEqual(response.status_code,201,response.content)
            status=self.client.get("/api/v1/hosting/status/")
        self.assertEqual(status.data["expiration_state"],"active")
        self.assertEqual(status.data["reported_space"]["quota_remaining_bytes"],"0")
        self.assertTrue(status.data["requires_attention"])

    def test_trigger_cannot_reuse_backup_encryption_key_even_when_configured_as_bytes(self):
        self.prepare()
        with override_settings(BACKUP_ENCRYPTION_KEY=b"h"*48):
            response=self.trigger(datetime(2026,11,3,tzinfo=utc.utc))
        self.assertEqual(response.status_code,503,response.content)
        self.assertEqual(HostingReminderAttempt.objects.count(),0)

    def test_explicit_retry_keeps_original_message_id_when_sender_domain_changes(self):
        self.prepare()
        at=datetime(2026,11,3,tzinfo=utc.utc)
        with patch("django.core.mail.message.EmailMessage.send",side_effect=TimeoutError()):
            self.assertEqual(self.trigger(at).data["status"],"uncertain")
        original=HostingReminderAttempt.objects.get()
        command={"operation_id":str(uuid.uuid4()),"expected_attempt_id":str(original.id),
            "possible_duplicate_confirmed":True,"reason":"Fournisseur SMTP changé, doublon possible accepté"}
        with override_settings(DEFAULT_FROM_EMAIL="new-sender@other.example.test"),patch("django.utils.timezone.now",return_value=at):
            response=self.client.post(f"/api/v1/hosting/reminders/{original.reminder_id}/retry/",command,format="json")
        self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.data["message_id"],original.message_id)
        self.assertEqual(mail.outbox[0].from_email,"new-sender@other.example.test")
        self.assertEqual(mail.outbox[0].extra_headers["Message-ID"],original.message_id)
