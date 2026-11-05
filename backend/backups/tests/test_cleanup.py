"""US-42: delete only this archive's files after every part is accepted."""
import json
import hashlib
from pathlib import Path
import uuid
from django.test import TransactionTestCase
from unittest.mock import patch
from django.db import connection
from backups.models import BackupJob, BackupPart, BackupEvent, SendAttempt
from backups.tests import test_sending as fixtures


class BackupCleanupTests(fixtures.BackupSendingFixtures, TransactionTestCase):
    def folder(self):
        return Path(self.directory.name)/str(self.job.id)

    def setUp(self):
        super().setUp()
        self.manifest = {"backup_id":str(self.job.id), "parts":[{"number":1,"sha256":self.part.sha256}]}
        self.job.manifest = self.manifest
        self.job.save(update_fields=["manifest"])
        (self.folder()/"manifest.json").write_text(json.dumps(self.manifest),encoding="utf-8")

    def test_last_smtp_acceptance_removes_archive_files_but_keeps_manifest_and_history(self):
        unrelated = self.folder()/"operator-note.txt"
        unrelated.write_text("keep",encoding="utf-8")
        attempt, _ = self.send()
        self.assertEqual(attempt.state,"accepted")
        self.assertFalse((self.folder()/self.part.filename).exists())
        self.job.refresh_from_db()
        self.assertEqual(self.job.cleanup_state,"cleaned")
        self.assertIsNotNone(self.job.cleaned_at)
        self.assertEqual(self.job.manifest,self.manifest)
        self.assertEqual(json.loads((self.folder()/"manifest.json").read_text(encoding="utf-8")),self.manifest)
        self.assertEqual(unrelated.read_text(encoding="utf-8"),"keep")
        self.assertEqual(BackupPart.objects.count(),1)
        self.assertEqual(SendAttempt.objects.count(),1)
        self.assertEqual(BackupEvent.objects.filter(kind="cleanup_completed").count(),1)
        replay,created=self.send(operation_id=attempt.operation_id)
        self.assertFalse(created)
        self.assertEqual(replay.id,attempt.id)
        self.assertEqual(len(self.submissions),1)

    def second_part(self):
        old=self.folder()/self.part.filename
        self.job.part_count=2
        self.job.save(update_fields=["part_count"])
        self.part.filename=f"backup-{self.job.id}.part-0001-of-0002.axbkp"
        self.part.save(update_fields=["filename"])
        old.rename(self.folder()/self.part.filename)
        other=BackupPart.objects.create(job=self.job,number=2,
            filename=f"backup-{self.job.id}.part-0002-of-0002.axbkp",size_bytes=100,sha256=hashlib.sha256(b"X"*100).hexdigest())
        (self.folder()/other.filename).write_bytes(b"X"*100)
        return other

    def test_any_missing_acceptance_preserves_all_parts_including_already_accepted_part(self):
        other=self.second_part()
        self.send()
        self.assertTrue((self.folder()/self.part.filename).exists())
        self.assertTrue((self.folder()/other.filename).exists())
        self.assertFalse(BackupEvent.objects.filter(kind="cleanup_completed").exists())

    def test_failed_or_uncertain_send_never_cleans_files(self):
        self.send(transport=self.reject)
        def timeout(message):
            raise TimeoutError()
        self.send(transport=timeout)
        self.assertTrue((self.folder()/self.part.filename).exists())
        self.assertFalse(BackupEvent.objects.filter(kind="cleanup_completed").exists())

    def test_replay_after_worker_crash_finishes_cleanup_without_another_smtp_submission(self):
        operation = uuid.uuid4()
        original = Path.unlink
        def stopped(path, *args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            original(path, *args, **kwargs)
            raise fixtures.StoppedWorker()
        with patch.object(Path,"unlink",autospec=True,side_effect=stopped):
            with self.assertRaises(fixtures.StoppedWorker):
                self.send(operation_id=operation)
        self.job.refresh_from_db()
        self.assertEqual(self.job.cleanup_state,"cleaning")
        self.assertEqual(SendAttempt.objects.get().state,"accepted")
        replay, created = self.send(operation_id=operation)
        self.assertFalse(created)
        self.assertEqual(replay.state,"accepted")
        self.job.refresh_from_db()
        self.assertEqual(self.job.cleanup_state,"cleaned")
        self.assertEqual(len(self.submissions),1)
        self.assertEqual(BackupEvent.objects.filter(kind="cleanup_completed").count(),1)

    def test_filesystem_failure_is_durable_and_replay_retries_cleanup_without_smtp(self):
        operation=uuid.uuid4()
        with patch.object(Path,"unlink",side_effect=PermissionError("private filesystem detail")):
            accepted, created=self.send(operation_id=operation)
        self.assertTrue(created)
        self.assertEqual(accepted.state,"accepted")
        self.job.refresh_from_db()
        self.assertEqual(self.job.cleanup_state,"failed")
        self.assertEqual(self.job.cleanup_error_code,"backup_cleanup_failed")
        self.assertTrue((self.folder()/self.part.filename).exists())
        self.assertNotIn("private",str(list(BackupEvent.objects.values())))
        self.send(operation_id=operation)
        self.job.refresh_from_db()
        self.assertEqual(self.job.cleanup_state,"cleaned")
        self.assertEqual(len(self.submissions),1)

    def test_forged_filename_never_deletes_a_file_outside_the_archive(self):
        with patch.object(Path,"unlink",side_effect=PermissionError()):
            self.send()
        victim=Path(self.directory.name)/"operator-note.txt"
        victim.write_text("must survive",encoding="utf-8")
        self.part.filename="../operator-note.txt"
        self.part.save(update_fields=["filename"])
        from backups.services.cleanup import cleanup_if_complete
        self.assertFalse(cleanup_if_complete(self.job.id))
        self.assertEqual(victim.read_text(encoding="utf-8"),"must survive")
        self.assertTrue((self.folder()/"manifest.json").exists())

    def test_partial_unlink_failure_can_finish_without_recreating_or_resending_missing_parts(self):
        from backups.services.sending import send_part
        other=self.second_part()
        self.send()
        original=Path.unlink
        def fail_second(path,*args,**kwargs):
            self.assertFalse(connection.in_atomic_block)
            if path.name==other.filename:
                raise PermissionError()
            return original(path,*args,**kwargs)
        operation=uuid.uuid4()
        with patch.object(Path,"unlink",autospec=True,side_effect=fail_second):
            accepted,created=send_part(self.operator,other.id,operation,transport=self.accept)
        self.assertTrue(created)
        self.assertEqual(accepted.state,"accepted")
        self.assertFalse((self.folder()/self.part.filename).exists())
        self.assertTrue((self.folder()/other.filename).exists())
        self.job.refresh_from_db()
        self.assertEqual(self.job.cleanup_state,"failed")
        replay,created=send_part(self.operator,other.id,operation,transport=self.accept)
        self.assertFalse(created)
        self.assertEqual(replay.id,accepted.id)
        self.assertFalse(list(self.folder().glob("*.axbkp")))
        self.assertTrue((self.folder()/"manifest.json").exists())
        self.assertEqual(len(self.submissions),2)
        self.assertEqual(BackupEvent.objects.filter(kind="cleanup_completed").count(),1)
