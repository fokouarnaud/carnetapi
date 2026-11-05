"""US-42: a prepared archive remains distinct from SMTP acceptance."""
import uuid

from django.db import connection, IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from backups.models import BackupJob, BackupPart
from identity.models import User


class BackupMetadataTests(TestCase):
    def setUp(self):
        self.technical = User.objects.create_superuser(username="backup-model", password=None)

    def job(self, **values):
        return BackupJob.objects.create(**{"operation_id": uuid.uuid4(), "requested_by": self.technical,
            "reason": "Sauvegarde contrôlée", "recipient": "backup@example.test", "key_id": "primary", **values})

    def test_preparation_is_exclusive_but_finished_archives_keep_their_history(self):
        first = self.job()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.job()
        first.state = "ready"
        first.snapshot_bytes = 4096
        first.archive_bytes = 1024
        first.part_count = 1
        first.prepared_at = timezone.now()
        first.save()
        second = self.job()
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(BackupJob.objects.count(), 2)
        first.refresh_from_db()
        self.assertEqual(first.state, "ready")

    def test_archive_cannot_be_marked_ready_without_complete_metadata(self):
        job = self.job()
        for state, failure in (("ready", ""), ("failed", ""), ("delivered", "")):
            with self.subTest(state=state), self.assertRaises(IntegrityError), transaction.atomic():
                BackupJob.objects.filter(id=job.id).update(state=state, failure_code=failure)

    def test_parts_are_identified_once_and_bounded_to_fifteen_decimal_megabytes(self):
        job = self.job()
        part = BackupPart.objects.create(job=job, number=1, filename="part-0001", size_bytes=15000000, sha256="a" * 64)
        with self.assertRaises(IntegrityError), transaction.atomic():
            BackupPart.objects.create(job=job, number=1, filename="other", size_bytes=10, sha256="b" * 64)
        for value in (0, 15000001, 10.5):
            with self.subTest(size=value), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("UPDATE backups_backuppart SET size_bytes = %s WHERE id = %s", [value, part.id.hex])

    def test_cleanup_state_cannot_claim_completion_before_preparation_or_without_dates(self):
        job = self.job()
        for values in (
            {"cleanup_state":"cleaned"},
            {"cleanup_state":"failed"},
            {"cleanup_state":"cleaning","cleanup_started_at":timezone.now()},
            {"cleanup_state":"waiting","cleaned_at":timezone.now()},
        ):
            with self.subTest(values=values),self.assertRaises(IntegrityError),transaction.atomic():
                BackupJob.objects.filter(id=job.id).update(**values)

    def test_abandonment_metadata_is_all_or_nothing_and_cannot_abandon_active_preparation(self):
        job=self.job()
        invalid=[
            {"abandon_reason":"missing author"},
            {"abandoned_at":timezone.now()},
            {"abandoned_at":timezone.now(),"abandoned_by":self.technical,
             "abandon_operation_id":uuid.uuid4(),"abandon_reason":"still preparing"},
        ]
        for values in invalid:
            with self.subTest(values=values),self.assertRaises(IntegrityError),transaction.atomic():
                BackupJob.objects.filter(id=job.id).update(**values)
