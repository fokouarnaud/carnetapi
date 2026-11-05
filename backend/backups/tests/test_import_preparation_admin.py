"""US-43: prepare a private copy from Unfold with current rights and real CSRF."""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TransactionTestCase
from django.urls import reverse
from backups.models import ImportPreparation
from backups.tests import test_import_preparation as fixtures


class ImportedPreparationAdministrationTests(TransactionTestCase):
    setUp = fixtures.ImportedArchivePreparationTests.setUp
    collect = fixtures.ImportedArchivePreparationTests.collect

    def test_prepare_form_and_result_are_bilingual_and_csrf_protected(self):
        self.collect()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.operator)
        url = reverse("admin:backups_restoreimport_prepare", args=[self.batch_id])
        for language, label in (("fr", "Préparer une copie de restauration"), ("en", "Prepare a restore copy")):
            with self.subTest(language=language):
                self.assertContains(client.get(url, HTTP_ACCEPT_LANGUAGE=language), label)
        body = {"operation_id": str(self.operation_id), "reason": "Copie depuis Unfold", "encryption_key": self.key.decode()}
        self.assertEqual(client.post(url, body).status_code, 403)
        response = client.post(url, {**body, "csrfmiddlewaretoken": client.cookies["csrftoken"].value})
        self.assertEqual(response.status_code, 302)
        record = ImportPreparation.objects.get()
        self.assertEqual(record.state, "prepared", record.failure_code)
        page = client.get(response.url, HTTP_ACCEPT_LANGUAGE="fr")
        self.assertContains(page, "Copie préparée")
        self.assertContains(page, record.candidate_sha256)
        self.assertContains(page, "Cette copie ne peut pas encore être activée.")
        self.assertNotContains(page, self.key.decode())
        self.assertNotContains(page, 'name="activate"')

    def test_current_business_staff_cannot_prepare_or_view_the_global_preparations(self):
        self.collect()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("admin:backups_restoreimport_prepare", args=[self.batch_id])).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:backups_importpreparation_changelist")).status_code, 403)
        self.assertEqual(self.client.generic("POST", reverse("admin:backups_restoreimport_prepare", args=[self.batch_id]),
            data=b"", CONTENT_LENGTH="999999999").status_code, 403)
        self.assertFalse(ImportPreparation.objects.exists())

    def test_missing_parts_or_uploaded_files_do_not_start_a_worker(self):
        self.collect(self.paths[:1])
        self.client.force_login(self.operator)
        url = reverse("admin:backups_restoreimport_prepare", args=[self.batch_id])
        body = {"operation_id": str(self.operation_id), "reason": "Contrôle", "encryption_key": self.key.decode()}
        self.assertEqual(self.client.post(url, body).status_code, 200)
        self.assertFalse(ImportPreparation.objects.exists())
        self.collect()
        response = self.client.post(url, {**body, "part": SimpleUploadedFile("unexpected", self.paths[0].read_bytes())})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ImportPreparation.objects.exists())
        self.assertNotContains(response, self.key.decode())
        self.assertEqual(self.client.generic("POST", url, data=b"", CONTENT_LENGTH="65537").status_code, 400)
