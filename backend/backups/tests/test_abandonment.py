"""Explicit abandonment preserves real events and prevents new SMTP attempts."""
from unittest.mock import patch
from pathlib import Path
import uuid
from django.test import TransactionTestCase
from rest_framework.test import APIClient
from backups.models import BackupEvent, SendAttempt
from backups.tests import test_sending as fixtures


class BackupAbandonmentTests(fixtures.BackupSendingFixtures,TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.api=APIClient()
        self.api.force_authenticate(self.operator)
        self.endpoint=f"/api/v1/backups/{self.job.id}/abandon/"

    def abandon(self, **values):
        return self.api.post(self.endpoint,{"operation_id":str(uuid.uuid4()),"reason":"Arrêt explicite après contrôle",**values},format="json")

    def test_abandonment_is_reasoned_audited_and_replayed_without_deleting_parts(self):
        self.send(transport=self.reject)
        operation=str(uuid.uuid4())
        response=self.abandon(operation_id=operation)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data["abandon_operation_id"],operation)
        self.assertIsNotNone(response.data["abandoned_at"])
        self.assertEqual(response.data["abandoned_by_id"],self.operator.id)
        self.assertEqual(response.data["abandon_reason"],"Arrêt explicite après contrôle")
        replay=self.abandon(operation_id=operation)
        self.assertEqual(replay.status_code,200)
        self.assertEqual(replay.data,response.data)
        self.assertEqual(self.abandon(operation_id=operation,reason="Autre décision").status_code,409)
        self.assertEqual(BackupEvent.objects.filter(job=self.job,kind="abandoned").count(),1)
        self.assertTrue((Path(self.directory.name)/str(self.job.id)/self.part.filename).exists())
        self.assertEqual(SendAttempt.objects.get().state,"failed")

    def test_abandoned_backup_blocks_new_submissions_but_preserves_old_receipt(self):
        first,_=self.send(transport=self.reject)
        self.assertEqual(self.abandon().status_code,200)
        with patch("backups.services.sending.submit_smtp",side_effect=self.accept):
            response=self.api.post(f"/api/v1/backups/{self.job.id}/parts/1/send/",
                {"operation_id":str(uuid.uuid4())},format="json")
            self.assertEqual(response.status_code,409)
            replay=self.api.post(f"/api/v1/backups/{self.job.id}/parts/1/send/",
                {"operation_id":str(first.operation_id)},format="json")
        self.assertEqual(replay.status_code,200)
        self.assertEqual(replay.data["id"],str(first.id))
        self.assertEqual(SendAttempt.objects.count(),1)
        self.assertEqual(len(self.submissions),1)

    def test_owner_blank_reason_and_already_complete_backup_cannot_be_abandoned(self):
        self.api.force_authenticate(self.owner)
        self.assertEqual(self.abandon().status_code,403)
        self.api.force_authenticate(self.operator)
        self.assertEqual(self.abandon(reason="   ").status_code,400)
        self.send()
        self.assertEqual(self.abandon().status_code,409)
        self.assertFalse(BackupEvent.objects.filter(kind="abandoned").exists())

    def test_late_smtp_acceptance_after_abandonment_is_preserved_without_cleanup(self):
        def late(message):
            self.submissions.append(message)
            self.assertEqual(self.abandon().status_code,200)
        attempt,_=self.send(transport=late)
        self.assertEqual(attempt.state,"accepted")
        self.job.refresh_from_db()
        self.assertIsNotNone(self.job.abandoned_at)
        self.assertEqual(self.job.cleanup_state,"waiting")
        self.assertTrue((Path(self.directory.name)/str(self.job.id)/self.part.filename).exists())
        self.assertEqual(BackupEvent.objects.filter(kind="abandoned").count(),1)
        self.assertEqual(BackupEvent.objects.filter(kind="smtp_accepted").count(),1)

    def test_abandonment_recorded_before_transport_prevents_that_submission(self):
        from rest_framework.exceptions import APIException
        original=BackupEvent.objects.create
        def abandoned_at_start(*args,**kwargs):
            event=original(*args,**kwargs)
            if kwargs.get("kind")=="smtp_started":
                self.assertEqual(self.abandon().status_code,200)
            return event
        with patch.object(BackupEvent.objects,"create",side_effect=abandoned_at_start):
            with self.assertRaises(APIException):
                self.send()
        self.assertFalse(self.submissions)
        attempt=SendAttempt.objects.get()
        self.assertEqual(attempt.state,"failed")
        self.assertEqual(attempt.error_code,"backup_abandoned")

    def test_unfold_abandonment_form_is_localized_and_preserves_explicit_decision(self):
        from django.urls import reverse
        self.client.force_login(self.operator)
        url=reverse("admin:backups_backupjob_abandon",args=[self.job.id])
        for language,label in (("fr","Abandonner cette sauvegarde"),("en","Abandon this backup")):
            with self.subTest(language=language):
                response=self.client.get(url,HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(response.status_code,200)
                self.assertContains(response,label)
                self.assertContains(response,str(self.job.id))
        response=self.client.post(url,{"operation_id":str(uuid.uuid4()),"reason":"Arrêt depuis Unfold"},HTTP_ACCEPT_LANGUAGE="fr")
        self.assertEqual(response.status_code,302)
        self.job.refresh_from_db()
        self.assertEqual(self.job.abandon_reason,"Arrêt depuis Unfold")
        self.assertEqual(self.job.abandoned_by_id,self.operator.id)
        self.owner.is_staff=True
        self.owner.save(update_fields=["is_staff"])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code,403)

    def test_same_operation_cannot_abandon_another_backup(self):
        from backups.models import BackupJob
        operation=str(uuid.uuid4())
        self.assertEqual(self.abandon(operation_id=operation).status_code,200)
        other=BackupJob.objects.create(operation_id=uuid.uuid4(),requested_by=self.operator,
            recipient="backup@example.test",reason="autre",key_id="primary",state="failed",failure_code="snapshot_failed")
        response=self.api.post(f"/api/v1/backups/{other.id}/abandon/",
            {"operation_id":operation,"reason":"Arrêt explicite après contrôle"},format="json")
        self.assertEqual(response.status_code,409)
        other.refresh_from_db()
        self.assertIsNone(other.abandoned_at)
        self.assertEqual(BackupEvent.objects.filter(kind="abandoned").count(),1)

    def test_session_revoked_after_authentication_blocks_abandonment(self):
        from datetime import timedelta
        from django.utils import timezone
        from identity.models import Installation,DeviceSession
        from identity.services.authentication import DeviceJWTAuthentication
        from rest_framework_simplejwt.tokens import AccessToken
        installation=Installation.objects.create(user=self.operator)
        session=DeviceSession.objects.create(installation=installation,expires_at=timezone.now()+timedelta(days=1))
        token=AccessToken.for_user(self.operator)
        token["session_id"]=str(session.id)
        api=APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        original=DeviceJWTAuthentication.authenticate
        def removed(authenticator,request):
            result=original(authenticator,request)
            DeviceSession.objects.filter(id=session.id).update(revoked_at=timezone.now())
            return result
        with patch.object(DeviceJWTAuthentication,"authenticate",autospec=True,side_effect=removed):
            response=api.post(self.endpoint,{"operation_id":str(uuid.uuid4()),"reason":"ne doit pas passer"},format="json")
        self.assertEqual(response.status_code,401)
        self.job.refresh_from_db()
        self.assertIsNone(self.job.abandoned_at)
        self.assertFalse(BackupEvent.objects.filter(kind="abandoned").exists())
