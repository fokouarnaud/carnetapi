"""A durable whole-archive command advances at most one SMTP part per step."""
from datetime import timedelta
import hashlib
from pathlib import Path
from unittest.mock import patch
import uuid
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient
from backups.models import BackupPart, BackupEvent, SendAttempt
from backups.tests import test_sending as fixtures


class BackupDispatchFixtures(fixtures.BackupSendingFixtures):
    def setUp(self):
        super().setUp()
        old=Path(self.directory.name)/str(self.job.id)/self.part.filename
        self.job.part_count=2
        self.job.save(update_fields=["part_count"])
        self.part.filename=f"backup-{self.job.id}.part-0001-of-0002.axbkp"
        self.part.save(update_fields=["filename"])
        old.rename(old.parent/self.part.filename)
        data=b"second encrypted part fixture"
        self.second=BackupPart.objects.create(job=self.job,number=2,
            filename=f"backup-{self.job.id}.part-0002-of-0002.axbkp",size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        (old.parent/self.second.filename).write_bytes(data)
        self.api=APIClient()
        self.api.force_authenticate(self.operator)

    def start(self,operation=None):
        return self.api.post(f"/api/v1/backups/{self.job.id}/dispatches/",
            {"operation_id":str(operation or uuid.uuid4()),"reason":"Envoi manuel de l’archive"},format="json")

    def advance(self,dispatch,transport=None):
        with patch("backups.services.sending.submit_smtp",side_effect=transport or self.accept):
            return self.api.post(f"/api/v1/backups/{self.job.id}/dispatches/{dispatch}/advance/",{},format="json")



class BackupDispatchTests(BackupDispatchFixtures, TransactionTestCase):
    def test_one_durable_command_sends_all_parts_in_bounded_steps_without_resending(self):
        operation=uuid.uuid4()
        started=self.start(operation)
        self.assertEqual(started.status_code,201)
        self.assertEqual(self.start(operation).data,started.data)
        self.assertFalse(self.submissions)
        dispatch=started.data["id"]
        first=self.advance(dispatch)
        self.assertEqual(first.status_code,200)
        self.assertEqual(first.data["state"],"active")
        self.assertEqual(len(self.submissions),1)
        second=self.advance(dispatch)
        self.assertEqual(second.data["state"],"accepted")
        self.assertEqual(second.data["accepted_parts"],2)
        self.assertEqual(len(self.submissions),2)
        self.assertEqual(self.advance(dispatch).data,second.data)
        self.assertEqual(len(self.submissions),2)
        self.job.refresh_from_db()
        self.assertEqual(self.job.cleanup_state,"cleaned")

    def test_failed_part_does_not_block_other_parts_or_bypass_retry_delay(self):
        started=self.start()
        self.assertEqual(started.status_code,201)
        dispatch=started.data["id"]
        self.advance(dispatch,self.reject)
        self.advance(dispatch)
        self.assertEqual(len(self.submissions),2)
        waiting=self.advance(dispatch)
        self.assertEqual(waiting.status_code,200)
        self.assertEqual(waiting.data["state"],"active")
        self.assertIsNotNone(waiting.data["next_attempt_at"])
        self.assertEqual(len(self.submissions),2)
        failed=SendAttempt.objects.get(state="failed")
        SendAttempt.objects.filter(id=failed.id).update(completed_at=timezone.now()-timedelta(minutes=16))
        completed=self.advance(dispatch)
        self.assertEqual(completed.data["state"],"accepted")
        self.assertEqual(len(self.submissions),3)
        self.assertEqual(SendAttempt.objects.filter(part=self.second).count(),1)

    def test_exhausted_retries_finish_incomplete_and_explicit_new_command_can_retry(self):
        started=self.start()
        self.assertEqual(started.status_code,201)
        dispatch=started.data["id"]
        self.advance(dispatch,self.reject)
        self.advance(dispatch)
        for number in (1,2,3):
            latest=SendAttempt.objects.filter(part=self.part).order_by("-number").first()
            SendAttempt.objects.filter(id=latest.id).update(completed_at=timezone.now()-timedelta(minutes=16))
            result=self.advance(dispatch,self.reject)
            self.assertEqual(SendAttempt.objects.filter(part=self.part).latest("number").retry_number,number)
        result=self.advance(dispatch)
        self.assertEqual(result.data["state"],"incomplete")
        self.assertEqual(len(self.submissions),5)
        manual=self.start()
        self.assertEqual(manual.status_code,201)
        self.assertNotEqual(manual.data["id"],dispatch)
        self.assertEqual(self.advance(manual.data["id"]).data["state"],"accepted")
        self.assertEqual(len(self.submissions),6)
        self.assertEqual(SendAttempt.objects.filter(part=self.part).latest("number").retry_number,0)

    def test_active_command_is_exclusive_and_owner_or_abandoned_job_is_refused(self):
        started=self.start()
        self.assertEqual(started.status_code,201)
        self.assertEqual(self.start().status_code,409)
        self.api.force_authenticate(self.owner)
        self.assertEqual(self.advance(started.data["id"]).status_code,403)
        self.api.force_authenticate(self.operator)
        abandoned=self.api.post(f"/api/v1/backups/{self.job.id}/abandon/",
            {"operation_id":str(uuid.uuid4()),"reason":"Arrêt"},format="json")
        self.assertEqual(abandoned.status_code,200)
        self.assertEqual(self.advance(started.data["id"]).data["state"],"abandoned")
        self.assertEqual(self.start().status_code,409)
        self.assertFalse(self.submissions)

    def test_dispatch_storage_rejects_two_active_commands_and_incomplete_lease_metadata(self):
        from django.apps import apps
        from django.db import IntegrityError,transaction
        model=apps.get_model("backups","BackupDispatch")
        command=model.objects.create(job=self.job,operation_id=uuid.uuid4(),requested_by=self.operator,reason="envoi")
        with self.assertRaises(IntegrityError),transaction.atomic():
            model.objects.create(job=self.job,operation_id=uuid.uuid4(),requested_by=self.operator,reason="autre")
        for values in ({"state":"accepted"},{"lease_id":uuid.uuid4()},
                {"leased_until":timezone.now()},{"state":"delivered","finished_at":timezone.now()}):
            with self.subTest(values=values),self.assertRaises(IntegrityError),transaction.atomic():
                model.objects.filter(id=command.id).update(**values)

    def test_configuration_failure_is_reported_without_submission_and_releases_lease(self):
        from django.test import override_settings
        from django.apps import apps
        started=self.start()
        self.assertEqual(started.status_code,201)
        with override_settings(BACKUP_EMAIL_MAX_BYTES=1):
            response=self.advance(started.data["id"])
        self.assertEqual(response.status_code,503)
        command=apps.get_model("backups","BackupDispatch").objects.get(id=started.data["id"])
        self.assertIsNone(command.lease_id)
        self.assertIsNone(command.leased_until)
        self.assertFalse(self.submissions)
        self.assertEqual(SendAttempt.objects.count(),0)
        self.assertEqual(self.advance(started.data["id"]).status_code,200)

    def test_unexpired_lease_blocks_another_worker_and_expired_lease_is_recoverable(self):
        from django.apps import apps
        model=apps.get_model("backups","BackupDispatch")
        started=self.start()
        self.assertEqual(started.status_code,201)
        command=started.data["id"]
        model.objects.filter(id=command).update(lease_id=uuid.uuid4(),leased_until=timezone.now()+timedelta(minutes=15))
        self.assertEqual(self.advance(command).status_code,409)
        self.assertFalse(self.submissions)
        model.objects.filter(id=command).update(leased_until=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.advance(command).status_code,200)
        self.assertEqual(len(self.submissions),1)
        self.assertIsNone(model.objects.get(id=command).lease_id)

    def test_interrupted_part_is_not_resent_until_its_uncertain_retry_is_due(self):
        started=self.start()
        self.assertEqual(started.status_code,201)
        dispatch=started.data["id"]
        def stopped(message):
            self.submissions.append(message)
            raise fixtures.StoppedWorker()
        with self.assertRaises(fixtures.StoppedWorker):
            self.advance(dispatch,stopped)
        self.assertEqual(SendAttempt.objects.get().state,"sending")
        self.advance(dispatch)
        self.assertEqual(len(self.submissions),2)
        first=SendAttempt.objects.get(part=self.part)
        self.advance(dispatch)
        self.assertEqual(len(self.submissions),2)
        SendAttempt.objects.filter(id=first.id).update(created_at=timezone.now()-timedelta(minutes=16))
        complete=self.advance(dispatch)
        self.assertEqual(complete.data["state"],"accepted")
        self.assertEqual(len(self.submissions),3)
        first.refresh_from_db()
        self.assertEqual(first.state,"uncertain")
        self.assertTrue(SendAttempt.objects.get(part=self.part,retry_number=1).possible_duplicate)

    def test_dispatch_followup_in_unfold_is_localized_readonly_and_technical_only(self):
        from django.urls import reverse
        started=self.start()
        self.assertEqual(started.status_code,201)
        self.client.force_login(self.operator)
        url=reverse("admin:backups_backupdispatch_changelist")
        for language,label in (("fr","Envoi de l’archive en cours"),("en","Dispatch active")):
            with self.subTest(language=language):
                response=self.client.get(url,HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(response.status_code,200)
                self.assertContains(response,label)
        self.assertEqual(self.client.get(reverse("admin:backups_backupdispatch_add")).status_code,403)
        self.owner.is_staff=True
        self.owner.save(update_fields=["is_staff"])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code,403)

    def test_completed_command_replay_can_finish_failed_cleanup_without_resending(self):
        started=self.start()
        self.assertEqual(started.status_code,201)
        dispatch=started.data["id"]
        with patch.object(Path,"unlink",side_effect=PermissionError()):
            self.advance(dispatch)
            completed=self.advance(dispatch)
        self.assertEqual(completed.data["state"],"accepted")
        self.job.refresh_from_db()
        self.assertEqual(self.job.cleanup_state,"failed")
        replay=self.advance(dispatch)
        self.assertEqual(replay.data["state"],"accepted")
        self.job.refresh_from_db()
        self.assertEqual(self.job.cleanup_state,"cleaned")
        self.assertEqual(len(self.submissions),2)
