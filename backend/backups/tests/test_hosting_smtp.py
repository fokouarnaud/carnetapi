"""Hosting reminder outcomes observed on an actual loopback SMTP connection."""
from datetime import datetime, timezone as utc
from email import policy
from email.parser import BytesParser
from threading import Thread
import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings
from backups.models import HostingReminder, HostingReminderAttempt
from backups.tests import test_hosting as fixtures
from verification.smtp_test_server import LocalSMTPServer, LocalSMTPHandler


class HostingSMTPTests(TestCase):
    configure=fixtures.HostingApiTests.configure
    trigger=fixtures.HostingApiTests.trigger

    def setUp(self):
        fixtures.HostingApiTests.setUp(self)
        self.server=LocalSMTPServer(("127.0.0.1",0),LocalSMTPHandler)
        self.server.mode,self.server.received="accept",[]
        self.worker=Thread(target=self.server.serve_forever,daemon=True);self.worker.start()
        self.addCleanup(self.close_server)
        options=override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1",EMAIL_PORT=self.server.server_address[1],EMAIL_USE_TLS=False,
            EMAIL_USE_SSL=False,EMAIL_HOST_USER="",EMAIL_HOST_PASSWORD="",EMAIL_TIMEOUT=2)
        options.enable();self.addCleanup(options.disable)
        with patch("django.utils.timezone.now",return_value=datetime(2026,11,1,tzinfo=utc.utc)):
            result,_=self.configure()
        self.assertEqual(result.status_code,200,result.content)

    def close_server(self):
        self.server.shutdown();self.server.server_close();self.worker.join(timeout=3)

    def test_data_acceptance_survives_lost_quit_reply_and_never_renews_hosting(self):
        self.server.mode="quit_drop"
        self.assertEqual(self.trigger(datetime(2026,11,3,tzinfo=utc.utc)).data["status"],"accepted")
        self.assertEqual(len(self.server.received),1)
        message=BytesParser(policy=policy.default).parsebytes(self.server.received[0])
        self.assertEqual(message["To"],"operator@example.test")
        self.assertIn("7 jours",message["Subject"])
        self.assertIn("renouvellement manuel",message.get_content())
        attempt=HostingReminderAttempt.objects.get()
        self.assertEqual((attempt.state,message["Message-ID"]),("accepted",attempt.message_id))
        configured=self.client.get("/api/v1/hosting/configuration/").data
        self.assertIsNone(configured["last_reported_renewal_at"])
        self.assertEqual(configured["expires_at"],"2026-11-10T15:00:00Z")

    def test_rejected_data_stays_failed_without_automatic_retry(self):
        self.server.mode="reject"
        at=datetime(2026,11,3,tzinfo=utc.utc)
        self.assertEqual(self.trigger(at).data["status"],"failed")
        self.assertEqual(self.trigger(at).data["status"],"idle")
        self.assertEqual(len(self.server.received),1)
        attempt=HostingReminderAttempt.objects.get()
        self.assertEqual((attempt.state,attempt.failure_code),("failed","smtp_rejected"))

    def test_lost_data_reply_stays_uncertain_until_explicit_audited_resend(self):
        self.server.mode="drop"
        at=datetime(2026,11,3,tzinfo=utc.utc)
        self.assertEqual(self.trigger(at).data["status"],"uncertain")
        self.assertEqual(self.trigger(at).data["status"],"idle")
        reminder=HostingReminder.objects.get(lead_days=7)
        command={"operation_id":str(uuid.uuid4()),"expected_attempt_id":str(reminder.current_attempt_id),
            "possible_duplicate_confirmed":True,"reason":"Réponse DATA perdue, renvoi manuel accepté"}
        self.server.mode="accept"
        with patch("django.utils.timezone.now",return_value=at):
            path=f"/api/v1/hosting/reminders/{reminder.id}/retry/"
            response=self.client.post(path,command,format="json")
            self.assertEqual((response.status_code,response.data["state"]),(201,"accepted"))
            self.assertEqual(self.client.post(path,command,format="json").status_code,200)
        self.assertEqual(len(self.server.received),2)
        messages=[BytesParser(policy=policy.default).parsebytes(item) for item in self.server.received]
        self.assertEqual(messages[0]["Message-ID"],messages[1]["Message-ID"])
        self.assertEqual(list(HostingReminderAttempt.objects.order_by("number").values_list("state",flat=True)),["uncertain","accepted"])
