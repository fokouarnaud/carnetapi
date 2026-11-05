"""Durable SMTP outcomes are not evidence that an email was delivered."""
import uuid
from django.apps import apps
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone
from backups.models import BackupJob, BackupPart
from identity.models import User


class SendAttemptStorageTests(TestCase):
    def setUp(self):
        self.operator = User.objects.create_superuser(username="smtp-attempt-operator", password=None)
        self.job = BackupJob.objects.create(operation_id=uuid.uuid4(), requested_by=self.operator,
            reason="test", recipient="backup@example.test", key_id="primary", state="ready",
            part_count=1, snapshot_bytes=4096, archive_bytes=100, prepared_at=timezone.now())
        self.part = BackupPart.objects.create(job=self.job, number=1, filename="part.axbkp", size_bytes=100, sha256="a" * 64)

    def attempt(self, **values):
        model = apps.get_model("backups", "SendAttempt")
        return model.objects.create(**{"part": self.part, "operation_id": uuid.uuid4(),
            "requested_by": self.operator, "number": 1, "retry_number": 0,
            "sender": "sender@example.test", "recipient": self.job.recipient,
            "encoded_bytes": 400, "wire_sha256": "b" * 64, **values})

    def test_only_one_inflight_attempt_per_part_and_one_receipt_per_operation(self):
        first = self.attempt()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.attempt(number=2)
        first.state, first.completed_at, first.error_code = "uncertain", timezone.now(), "smtp_response_unknown"
        first.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.attempt(number=2, operation_id=first.operation_id)
        second = self.attempt(number=2, retry_number=1, previous=first)
        self.assertNotEqual(first.id, second.id)
        first.refresh_from_db()
        self.assertEqual(first.state, "uncertain")

    def test_unknown_outcomes_and_inconsistent_completion_are_rejected(self):
        first = self.attempt()
        invalid = [
            {"state": "delivered"},
            {"state": "accepted"},
            {"state": "failed", "completed_at": timezone.now()},
            {"state": "uncertain", "completed_at": timezone.now()},
            {"state": "sending", "completed_at": timezone.now()},
            {"state": "accepted", "completed_at": timezone.now(), "error_code": "smtp_rejected"},
        ]
        model = type(first)
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                model.objects.filter(id=first.id).update(**values)

    def test_attempt_numbers_retry_budget_and_encoded_size_are_exact_integers(self):
        first = self.attempt()
        for column, value in (("number",0),("number",1.5),("retry_number",4),
                ("retry_number",-1),("retry_number",0.5),("encoded_bytes",0),("encoded_bytes",400.5)):
            with self.subTest(column=column,value=value), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f'UPDATE backups_sendattempt SET "{column}"=%s WHERE id=%s',[value,first.id.hex])
