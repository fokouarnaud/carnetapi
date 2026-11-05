"""US-43: Unfold quarantines a private copy with current rights and CSRF."""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TransactionTestCase
from django.urls import reverse
from backups.models import ImportQuarantine
from backups.tests import test_import_preparation as fixtures
from backups.tests import test_import_quarantine as service_tests
from identity.models import User


class ImportQuarantineAdministrationTests(TransactionTestCase):
    setUp = fixtures.ImportedArchivePreparationTests.setUp
    collect = fixtures.ImportedArchivePreparationTests.collect
    prepare = fixtures.ImportedArchivePreparationTests.prepare
    prepared_source = service_tests.ImportedQuarantineTests.prepared_source

    def test_form_result_and_replay_are_bilingual_csrf_protected_and_do_not_activate(self):
        source = self.prepared_source()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.operator)
        url = reverse("admin:backups_importquarantine_start", args=[source.id])
        for language, label in (("fr", "Mettre la copie en quarantaine"), ("en", "Quarantine the copy")):
            with self.subTest(language=language):
                self.assertContains(client.get(url, HTTP_ACCEPT_LANGUAGE=language), label)
        self.assertFalse(ImportQuarantine.objects.exists())
        body = {"operation_id":str(self.operation_id),"reason":"Suspendre les accès archivés"}
        self.assertEqual(client.post(url, body).status_code, 403)
        body["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
        response = client.post(url, body)
        self.assertEqual(response.status_code, 302)
        record = ImportQuarantine.objects.get()
        self.assertEqual(record.state, "quarantined", record.failure_code)
        self.assertEqual(response.url, reverse("admin:backups_importquarantine_result", args=[record.id]))
        page = client.get(response.url, HTTP_ACCEPT_LANGUAGE="fr")
        self.assertContains(page, "Copie en quarantaine")
        self.assertContains(page, "Cette copie ne peut pas encore être activée.")
        self.assertContains(page, record.candidate_sha256)
        self.assertNotContains(page, self.key.decode())
        self.assertNotContains(page, 'name="activate"')
        self.assertEqual(client.post(url, body).url, response.url)
        self.assertEqual(ImportQuarantine.objects.count(), 1)

    def test_business_staff_and_removed_technical_role_cannot_read_or_start_quarantine(self):
        source = self.prepared_source()
        url = reverse("admin:backups_importquarantine_start", args=[source.id])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.generic("POST",url,data=b"",CONTENT_LENGTH="999999999").status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:backups_importquarantine_changelist")).status_code, 403)
        self.assertFalse(ImportQuarantine.objects.exists())
        self.client.force_login(self.operator)
        response = self.client.post(url, {"operation_id":str(self.operation_id),"reason":"Droits vérifiés"})
        self.assertEqual(response.status_code, 302)
        User.objects.filter(id=self.operator.id).update(is_superuser=False)
        self.assertEqual(self.client.get(response.url).status_code, 403)

    def test_large_body_or_file_does_not_start_quarantine(self):
        source = self.prepared_source()
        url = reverse("admin:backups_importquarantine_start", args=[source.id])
        self.client.force_login(self.operator)
        self.assertEqual(self.client.generic("POST",url,data=b"",CONTENT_LENGTH="65537").status_code, 400)
        response = self.client.post(url, {"operation_id":str(self.operation_id),"reason":"Pas de fichier attendu",
            "part":SimpleUploadedFile("unexpected",b"unwanted")}, HTTP_ACCEPT_LANGUAGE="en")
        self.assertContains(response, "Unable to quarantine this copy.")
        self.assertFalse(ImportQuarantine.objects.exists())
