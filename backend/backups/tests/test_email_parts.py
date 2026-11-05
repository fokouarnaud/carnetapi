"""US-42: inspect the exact SMTP bytes without sending any email."""
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import uuid

from django.core import mail
from django.test import SimpleTestCase, override_settings


class BackupEmailTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="backup-email-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.job = SimpleNamespace(id=uuid.UUID("a324072f-aa61-40cb-ab37-9928f482f3ff"),
            state="ready", recipient="manual@example.test", part_count=2,
            created_at=datetime(2026, 10, 19, 1, 0, tzinfo=timezone.utc))
        self.data = b"APPXBKP1" + bytes(range(256)) * 16
        self.part = SimpleNamespace(number=1, job_id=self.job.id,
            filename=f"backup-{self.job.id}.part-0001-of-0002.axbkp",
            size_bytes=len(self.data), sha256=hashlib.sha256(self.data).hexdigest())
        self.folder = self.root / str(self.job.id)
        self.folder.mkdir()
        self.path = self.folder / self.part.filename
        self.path.write_bytes(self.data)
        self.options = override_settings(BACKUP_ROOT=self.root)
        self.options.enable()
        self.addCleanup(self.options.disable)

    def build(self, **kwargs):
        from backups.services.email_parts import build_part_email
        return build_part_email(self.job, self.part, sender="sender@example.test",
            max_message_bytes=kwargs.pop("max_message_bytes", 25000000), **kwargs)

    def test_wire_message_identifies_backup_and_preserves_one_encrypted_attachment(self):
        result = self.build()
        message = BytesParser(policy=policy.SMTP).parsebytes(result.wire_bytes)
        self.assertEqual(result.sender, "sender@example.test")
        self.assertEqual(result.recipient, "manual@example.test")
        self.assertEqual(message["To"], "manual@example.test")
        self.assertEqual(message["From"], "sender@example.test")
        self.assertIn(str(self.job.id), str(message["Subject"]))
        self.assertIn("1/2", str(message["Subject"]))
        self.assertEqual(message["Date"].datetime, self.job.created_at)
        self.assertIn(str(self.job.id), str(message["Message-ID"]))
        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(), self.part.filename)
        self.assertEqual(attachments[0].get_payload(decode=True), self.data)
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn(self.part.sha256, body)
        self.assertIn("SMTP", body)
        self.assertEqual(len(mail.outbox), 0)
        self.assertNotIn(b"\n", result.wire_bytes.replace(b"\r\n", b""))

    def test_checks_encoded_message_size_including_mime_headers(self):
        from backups.services.email_parts import BackupEmailError
        original = self.build()
        self.assertGreater(len(original.wire_bytes), self.part.size_bytes)
        exact = self.build(max_message_bytes=len(original.wire_bytes))
        self.assertEqual(len(exact.wire_bytes), len(original.wire_bytes))
        with self.assertRaisesMessage(BackupEmailError, "backup_email_too_large"):
            self.build(max_message_bytes=len(original.wire_bytes) - 1)

    def test_rejects_modified_encrypted_file_before_preparing_email(self):
        from backups.services.email_parts import BackupEmailError
        self.path.write_bytes(b"X" + self.data[1:])
        with self.assertRaisesMessage(BackupEmailError, "backup_part_integrity_failed"):
            self.build()

    def test_recipient_cannot_inject_an_extra_header_or_multiple_recipients(self):
        from backups.services.email_parts import BackupEmailError
        for value in ("victim@example.test\r\nBcc: leak@example.test", "a@example.test,b@example.test"):
            with self.subTest(recipient=value):
                self.job.recipient = value
                with self.assertRaisesMessage(BackupEmailError, "backup_email_address_invalid"):
                    self.build()

    def test_preparing_job_and_invalid_part_number_cannot_be_mailed(self):
        from backups.services.email_parts import BackupEmailError
        self.job.state = "preparing"
        with self.assertRaisesMessage(BackupEmailError, "backup_email_not_ready"):
            self.build()
        self.job.state = "ready"
        self.part.number = 3
        with self.assertRaisesMessage(BackupEmailError, "backup_email_part_invalid"):
            self.build()

    def test_part_of_another_backup_is_rejected_even_when_local_bytes_match(self):
        from backups.services.email_parts import BackupEmailError
        self.part.job_id = uuid.uuid4()
        with self.assertRaisesMessage(BackupEmailError, "backup_email_part_invalid"):
            self.build()

    def test_fifteen_megabyte_part_is_checked_after_base64_expansion(self):
        from backups.services.email_parts import BackupEmailError
        data = b"X" * 15000000
        self.part.size_bytes = len(data)
        self.part.sha256 = hashlib.sha256(data).hexdigest()
        self.path.write_bytes(data)
        with self.assertRaisesMessage(BackupEmailError, "backup_email_too_large"):
            self.build(max_message_bytes=15000000)
        result = self.build(max_message_bytes=25000000)
        self.assertGreater(len(result.wire_bytes), 20000000)
        self.assertLessEqual(len(result.wire_bytes), 25000000)
        parsed = BytesParser(policy=policy.SMTP).parsebytes(result.wire_bytes)
        self.assertEqual(next(parsed.iter_attachments()).get_payload(decode=True), data)
