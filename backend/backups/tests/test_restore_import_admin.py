"""Technical Unfold collection is CSRF protected and never an activation screen."""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TransactionTestCase
from django.urls import reverse
from backups.models import RestoreImport
from backups.tests import test_restore_imports as fixtures


class RestoreImportAdministrationTests(TransactionTestCase):
    setUp = fixtures.RestoreCollectionTests.setUp

    def test_technical_form_collects_one_part_and_localizes_inventory(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.operator)
        start = reverse("admin:backups_restoreimport_start")
        for language, title in (("fr", "Importer des parties de sauvegarde"), ("en", "Import backup parts")):
            with self.subTest(language=language):
                response = self.client.get(start, HTTP_ACCEPT_LANGUAGE=language)
                self.assertContains(response, title)
        response = self.client.post(start, {"batch_id": str(self.batch_id), "reason": "Reprise contrôlée",
            "csrfmiddlewaretoken": self.client.cookies["csrftoken"].value,
            "encryption_key": self.key.decode(), "part": SimpleUploadedFile("part.axbkp", self.paths[1].read_bytes())})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(RestoreImport.objects.count(), 1)
        page = self.client.get(response.url, HTTP_ACCEPT_LANGUAGE="fr")
        self.assertContains(page, "Parties manquantes")
        self.assertContains(page, "Vérification complète encore requise")
        self.assertNotContains(page, self.key.decode())
        self.assertNotContains(page, 'name="activate"')
        self.assertEqual(RestoreImport.objects.get().parts.count(), 1)

    def test_owner_cannot_access_collection_or_generic_admin_writes(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("admin:backups_restoreimport_start")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:backups_restoreimport_changelist")).status_code, 403)
        self.assertEqual(self.client.generic("POST", reverse("admin:backups_restoreimport_start"),
            data=b"", content_type="multipart/form-data; boundary=refuse", CONTENT_LENGTH="999999999").status_code, 403)
        self.assertFalse(self.storage.exists())
        self.client.force_login(self.operator)
        self.assertEqual(self.client.post(reverse("admin:backups_restoreimport_add"), {}).status_code, 403)

    def test_csrf_and_wrong_key_never_collect_a_part_or_echo_the_key(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.operator)
        start = reverse("admin:backups_restoreimport_start")
        data = {"batch_id": str(self.batch_id), "reason": "Reprise contrôlée",
            "encryption_key": self.key.decode(), "part": SimpleUploadedFile("part.axbkp", self.paths[0].read_bytes())}
        self.assertEqual(client.post(start, data).status_code, 403)
        self.client.force_login(self.operator)
        from cryptography.fernet import Fernet
        wrong = Fernet.generate_key().decode()
        response = self.client.post(start, {**data, "encryption_key": wrong,
            "part": SimpleUploadedFile("part.axbkp", self.paths[0].read_bytes())})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, wrong)
        self.assertFalse(RestoreImport.objects.exists())
        from django.views.debug import SafeExceptionReporterFilter
        filtered = SafeExceptionReporterFilter().get_post_parameters(response.wsgi_request)
        self.assertNotEqual(filtered.get("encryption_key"), wrong)
        self.assertEqual(filtered.get("reason"), "Reprise contrôlée")

    def test_readonly_import_metadata_uses_french_labels(self):
        self.client.force_login(self.operator)
        batch, part, _created = fixtures.RestoreCollectionTests.collect(self, 0)
        page = self.client.get(reverse("admin:backups_restoreimport_change", args=[batch.id]), HTTP_ACCEPT_LANGUAGE="fr")
        self.assertContains(page, "Créé le (UTC)")
        self.assertContains(page, "SHA-256 des métadonnées")
        part_page = self.client.get(reverse("admin:backups_restoreimportpart_change", args=[part.id]), HTTP_ACCEPT_LANGUAGE="fr")
        self.assertContains(part_page, "Modifié le (UTC)")

    def test_multiple_files_or_oversized_body_leave_no_part_in_storage(self):
        self.client.force_login(self.operator)
        start = reverse("admin:backups_restoreimport_start")
        response = self.client.post(start, {"batch_id": str(self.batch_id), "reason": "Refus multiple",
            "encryption_key": self.key.decode(), "part": [
                SimpleUploadedFile("first", self.paths[0].read_bytes()),
                SimpleUploadedFile("second", self.paths[1].read_bytes())]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(RestoreImport.objects.exists())
        self.assertFalse(list(self.storage.rglob("*.axbkp")))
        denied = self.client.generic("POST", start, data=b"", content_type="multipart/form-data; boundary=limit",
            CONTENT_LENGTH="999999999")
        self.assertEqual(denied.status_code, 400)
        self.assertFalse(RestoreImport.objects.exists())
