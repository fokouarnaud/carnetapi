"""Real smtplib exchange against an isolated loopback SMTP test server."""
import smtplib
from threading import Thread
from django.test import SimpleTestCase, override_settings
from backups.services.email_parts import PreparedPartEmail
from backups.services.sending import submit_smtp
from verification.smtp_test_server import LocalSMTPServer, LocalSMTPHandler


class SMTPWireTests(SimpleTestCase):
    def setUp(self):
        self.server = LocalSMTPServer(("127.0.0.1",0),LocalSMTPHandler)
        self.server.mode, self.server.received = "accept", []
        self.worker = Thread(target=self.server.serve_forever,daemon=True)
        self.worker.start()
        self.addCleanup(self.close_server)
        options=override_settings(EMAIL_HOST="127.0.0.1",EMAIL_PORT=self.server.server_address[1],
            EMAIL_USE_TLS=False,EMAIL_USE_SSL=False,EMAIL_HOST_USER="",EMAIL_HOST_PASSWORD="",EMAIL_TIMEOUT=2)
        options.enable()
        self.addCleanup(options.disable)
        self.message=PreparedPartEmail("sender@example.test","recipient@example.test",
            b"From: sender@example.test\r\nTo: recipient@example.test\r\nSubject: local check\r\n\r\n.Payload\r\n")

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)

    def test_real_smtp_data_preserves_measured_bytes(self):
        submit_smtp(self.message)
        self.assertEqual(self.server.received,[self.message.wire_bytes])

    def test_explicit_rejection_is_distinct_from_lost_data_response(self):
        self.server.mode="reject"
        with self.assertRaises(smtplib.SMTPDataError):
            submit_smtp(self.message)
        self.server.mode="drop"
        with self.assertRaises(smtplib.SMTPServerDisconnected):
            submit_smtp(self.message)
        self.assertEqual(self.server.received,[self.message.wire_bytes]*2)

    def test_lost_quit_response_does_not_erase_known_data_acceptance(self):
        self.server.mode="quit_drop"
        submit_smtp(self.message)
        self.assertEqual(self.server.received,[self.message.wire_bytes])
