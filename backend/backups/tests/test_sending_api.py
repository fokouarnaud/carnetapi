"""HTTP access to durable part submissions; SMTP boundary is isolated."""
from unittest.mock import patch
import uuid
from rest_framework.test import APIClient
from django.test import TransactionTestCase
from backups.tests import test_sending as fixtures
from backups.models import SendAttempt


class BackupSendingApiTests(fixtures.BackupSendingFixtures, TransactionTestCase):
    def test_send_endpoint_records_acceptance_and_replay_and_lists_attempts(self):
        api = APIClient()
        api.force_authenticate(self.operator)
        endpoint = f"/api/v1/backups/{self.job.id}/parts/1/send/"
        operation = str(uuid.uuid4())
        with patch("backups.services.sending.submit_smtp", side_effect=self.accept):
            response = api.post(endpoint, {"operation_id": operation}, format="json")
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.data["state"], "accepted")
            replay = api.post(endpoint, {"operation_id": operation}, format="json")
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.data, response.data)
        history = api.get(f"/api/v1/backups/{self.job.id}/parts/1/attempts/")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.data["count"], 1)
        self.assertEqual(history.data["results"][0]["state"], "accepted")
        self.assertEqual(len(self.submissions), 1)

    def test_owner_cannot_send_or_read_attempt_history(self):
        api = APIClient()
        api.force_authenticate(self.owner)
        with patch("backups.services.sending.submit_smtp", side_effect=self.accept):
            self.assertEqual(api.post(f"/api/v1/backups/{self.job.id}/parts/1/send/",
                {"operation_id": str(uuid.uuid4())}, format="json").status_code, 403)
        self.assertEqual(api.get(f"/api/v1/backups/{self.job.id}/parts/1/attempts/").status_code, 403)
        self.assertFalse(SendAttempt.objects.exists())
        self.assertFalse(self.submissions)

    def test_invalid_message_is_503_and_no_smtp_attempt_is_recorded(self):
        from django.test import override_settings
        api = APIClient()
        api.force_authenticate(self.operator)
        with override_settings(BACKUP_EMAIL_MAX_BYTES=1), patch("backups.services.sending.submit_smtp", side_effect=self.accept):
            response = api.post(f"/api/v1/backups/{self.job.id}/parts/1/send/",
                {"operation_id": str(uuid.uuid4())}, format="json")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(SendAttempt.objects.exists())
        self.assertFalse(self.submissions)


    def test_unfold_shows_actual_smtp_outcome_in_french_and_english_readonly(self):
        from django.urls import reverse
        self.send()
        self.client.force_login(self.operator)
        for language, label in (("fr", "Acceptée par SMTP, livraison non confirmée"),
                ("en", "Accepted by SMTP, delivery unconfirmed")):
            with self.subTest(language=language):
                response = self.client.get(reverse("admin:backups_sendattempt_changelist"), HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, label)
                self.assertNotContains(response, "name=\"action\"")
        jobs = self.client.get(reverse("admin:backups_backupjob_changelist"), HTTP_ACCEPT_LANGUAGE="fr")
        self.assertNotContains(jobs, "Préparée, non envoyée")
        self.assertContains(jobs, "Fichiers de l’archive nettoyés")
        self.owner.is_staff = True
        self.owner.save(update_fields=["is_staff"])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("admin:backups_sendattempt_changelist")).status_code,403)

    def test_cleaned_archive_remains_auditable_and_download_explains_gone_files(self):
        api=APIClient()
        api.force_authenticate(self.operator)
        operation=str(uuid.uuid4())
        with patch("backups.services.sending.submit_smtp",side_effect=self.accept):
            accepted=api.post(f"/api/v1/backups/{self.job.id}/parts/1/send/",{"operation_id":operation},format="json")
        self.assertEqual(accepted.status_code,201)
        detail=api.get(f"/api/v1/backups/{self.job.id}/")
        self.assertEqual(detail.status_code,200)
        self.assertEqual(detail.data["cleanup_state"],"cleaned")
        self.assertIsNotNone(detail.data["cleaned_at"])
        gone=api.get(f"/api/v1/backups/{self.job.id}/parts/1/")
        self.assertEqual(gone.status_code,410)
        self.assertEqual(gone.data["code"],"backup_archive_cleaned")
        api.force_authenticate(self.owner)
        self.assertEqual(api.get(f"/api/v1/backups/{self.job.id}/parts/1/").status_code,403)
