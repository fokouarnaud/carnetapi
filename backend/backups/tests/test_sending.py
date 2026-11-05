"""US-42: replay, bounded retries and uncertain submissions preserve history."""
from datetime import timedelta
import hashlib
from pathlib import Path
import smtplib
from tempfile import TemporaryDirectory
import uuid

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import APIException, PermissionDenied

from backups.models import BackupJob, BackupPart, SendAttempt
from identity.models import User


class StoppedWorker(BaseException):
    pass


class BackupSendingFixtures:
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="backup-sending-")
        self.addCleanup(self.directory.cleanup)
        self.options = override_settings(BACKUP_ROOT=Path(self.directory.name),
            EMAIL_HOST_USER="sender@example.test", BACKUP_EMAIL_MAX_BYTES=25000000)
        self.options.enable()
        self.addCleanup(self.options.disable)
        self.operator = User.objects.create_superuser(username="backup-sending", password=None)
        self.owner = User.objects.create_user(username="business-owner")
        self.job = BackupJob.objects.create(operation_id=uuid.uuid4(), requested_by=self.operator,
            reason="test", recipient="backup@example.test", key_id="primary", state="ready",
            part_count=1, snapshot_bytes=4096, archive_bytes=100, prepared_at=timezone.now())
        data = b"APPXBKP1" + bytes(range(256))
        self.part = BackupPart.objects.create(job=self.job, number=1,
            filename=f"backup-{self.job.id}.part-0001-of-0001.axbkp",
            size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        folder = Path(self.directory.name) / str(self.job.id)
        folder.mkdir()
        (folder/self.part.filename).write_bytes(data)
        self.submissions = []

    def accept(self, message):
        self.assertFalse(connection.in_atomic_block)
        self.assertEqual(SendAttempt.objects.filter(state="sending", wire_sha256=hashlib.sha256(message.wire_bytes).hexdigest()).count(), 1)
        self.submissions.append(message)

    def reject(self, message):
        self.submissions.append(message)
        raise smtplib.SMTPDataError(550, b"untrusted private SMTP response")

    def send(self, **values):
        from backups.services.sending import send_part
        return send_part(values.pop("actor", self.operator), self.part.id,
            values.pop("operation_id", uuid.uuid4()), transport=values.pop("transport", self.accept), **values)



class BackupSendingTests(BackupSendingFixtures, TransactionTestCase):
    def test_acceptance_is_durable_and_replay_or_new_request_does_not_resend(self):
        operation = uuid.uuid4()
        first, created = self.send(operation_id=operation)
        self.assertTrue(created)
        self.assertEqual(first.state, "accepted")
        self.assertIsNotNone(first.completed_at)
        self.assertEqual(first.error_code, "")
        self.assertEqual(first.wire_sha256, hashlib.sha256(self.submissions[0].wire_bytes).hexdigest())
        replay, created = self.send(operation_id=operation)
        self.assertFalse(created)
        self.assertEqual(replay.id, first.id)
        accepted, created = self.send()
        self.assertFalse(created)
        self.assertEqual(accepted.id, first.id)
        self.assertEqual(len(self.submissions), 1)
        self.assertEqual(SendAttempt.objects.count(), 1)

    def test_explicit_smtp_rejection_is_failed_and_response_loss_is_uncertain(self):
        failed, _ = self.send(transport=self.reject)
        self.assertEqual(failed.state, "failed")
        self.assertEqual(failed.error_code, "smtp_rejected")
        def timeout(message):
            self.submissions.append(message)
            raise TimeoutError("untrusted private timeout detail")
        uncertain, _ = self.send(transport=timeout)
        self.assertEqual(uncertain.state, "uncertain")
        self.assertEqual(uncertain.error_code, "smtp_acceptance_unknown")
        self.assertIsNotNone(uncertain.completed_at)
        self.assertNotIn("private", str(list(SendAttempt.objects.values())))

    def make_due(self, attempt):
        SendAttempt.objects.filter(id=attempt.id).update(completed_at=timezone.now()-timedelta(minutes=15,seconds=1))

    def test_automatic_retries_wait_fifteen_minutes_and_stop_after_three_new_attempts(self):
        first, _ = self.send(transport=self.reject)
        with self.assertRaises(APIException):
            self.send(retry_of=first.id, transport=self.reject)
        self.assertEqual(len(self.submissions), 1)
        previous = first
        for number in (1, 2, 3):
            self.make_due(previous)
            current, created = self.send(retry_of=previous.id, transport=self.reject)
            self.assertTrue(created)
            self.assertEqual(current.retry_number, number)
            self.assertEqual(current.previous_id, previous.id)
            previous = current
        self.make_due(previous)
        with self.assertRaises(APIException):
            self.send(retry_of=previous.id, transport=self.reject)
        self.assertEqual(len(self.submissions), 4)
        self.assertEqual(SendAttempt.objects.count(), 4)

    def test_crashed_worker_remains_uncertain_and_can_be_retried_only_after_lease(self):
        def stopped(message):
            self.submissions.append(message)
            raise StoppedWorker()
        with self.assertRaises(StoppedWorker):
            self.send(transport=stopped)
        first = SendAttempt.objects.get()
        self.assertEqual(first.state, "sending")
        with self.assertRaises(APIException):
            self.send(retry_of=first.id)
        SendAttempt.objects.filter(id=first.id).update(created_at=timezone.now()-timedelta(minutes=15,seconds=1))
        retried, _ = self.send(retry_of=first.id)
        first.refresh_from_db()
        self.assertEqual(first.state, "uncertain")
        self.assertEqual(first.error_code, "smtp_acceptance_unknown")
        self.assertEqual(retried.state, "accepted")
        self.assertEqual(retried.retry_number, 1)
        self.assertEqual(len(self.submissions), 2)

    def test_owner_and_removed_technical_user_cannot_submit_or_replay(self):
        with self.assertRaises(PermissionDenied):
            self.send(actor=self.owner)
        operation = uuid.uuid4()
        self.send(operation_id=operation)
        User.objects.filter(id=self.operator.id).update(is_superuser=False)
        with self.assertRaises(PermissionDenied):
            self.send(operation_id=operation)
        self.assertEqual(len(self.submissions), 1)

    def test_broken_or_oversized_part_creates_no_attempt_and_never_calls_smtp(self):
        from backups.services.email_parts import BackupEmailError
        with override_settings(BACKUP_EMAIL_MAX_BYTES=1):
            with self.assertRaisesMessage(BackupEmailError, "backup_email_too_large"):
                self.send()
        self.assertFalse(SendAttempt.objects.exists())
        self.assertFalse(self.submissions)

    def test_invalid_retry_operation_cannot_reuse_original_operation_uuid(self):
        operation = uuid.uuid4()
        first, _ = self.send(operation_id=operation, transport=self.reject)
        self.make_due(first)
        with self.assertRaises(APIException):
            self.send(operation_id=operation, retry_of=first.id)
        self.assertEqual(len(self.submissions), 1)

    def test_automatic_retry_at_exact_fifteen_minute_boundary_is_allowed(self):
        from unittest.mock import patch
        first, _ = self.send(transport=self.reject)
        due = first.completed_at + timedelta(minutes=15)
        with patch("backups.services.sending.timezone.now", return_value=due-timedelta(microseconds=1)):
            with self.assertRaises(APIException):
                self.send(retry_of=first.id)
        with patch("backups.services.sending.timezone.now", return_value=due):
            retried, _ = self.send(retry_of=first.id)
        self.assertEqual(retried.state, "accepted")
        self.assertEqual(len(self.submissions), 2)

    def test_late_original_acceptance_and_accepted_retry_are_both_preserved(self):
        def late_acceptance(message):
            self.submissions.append(message)
            original = SendAttempt.objects.get(state="sending")
            SendAttempt.objects.filter(id=original.id).update(created_at=timezone.now()-timedelta(minutes=16))
            retry, _ = self.send(retry_of=original.id)
            self.assertEqual(retry.state, "accepted")
        original, _ = self.send(transport=late_acceptance)
        self.assertEqual(original.state, "accepted")
        self.assertEqual(SendAttempt.objects.filter(state="accepted").count(), 2)
        self.assertEqual(len(self.submissions), 2)
        self.assertEqual(self.job.parts.count(), 1)
        from email import policy
        from email.parser import BytesParser
        warning = BytesParser(policy=policy.SMTP).parsebytes(self.submissions[1].wire_bytes).get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Possible duplicate", warning)
        self.assertTrue(SendAttempt.objects.get(retry_number=1).possible_duplicate)

    def test_explicit_manual_relaunch_after_unknown_acceptance_warns_of_possible_duplicate(self):
        def timeout(message):
            self.submissions.append(message)
            raise TimeoutError()
        self.send(transport=timeout)
        accepted,_=self.send()
        self.assertTrue(accepted.possible_duplicate)
        from email import policy
        from email.parser import BytesParser
        body=BytesParser(policy=policy.SMTP).parsebytes(self.submissions[-1].wire_bytes).get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Possible duplicate",body)
