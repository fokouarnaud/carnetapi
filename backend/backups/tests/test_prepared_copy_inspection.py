"""US-43: recheck the surviving copy, rather than trusting its recorded size."""
from contextlib import closing
import hashlib
import sqlite3
from django.test import Client, TransactionTestCase
from django.urls import reverse
from rest_framework.exceptions import PermissionDenied
from backups.services.restore_imports import RestoreImportError
from backups.tests import test_import_preparation as fixtures


class PreparedCopyInspectionTests(TransactionTestCase):
    setUp = fixtures.ImportedArchivePreparationTests.setUp
    collect = fixtures.ImportedArchivePreparationTests.collect
    prepare = fixtures.ImportedArchivePreparationTests.prepare

    def prepared(self):
        from backups.services.import_preparation import preparation_directory
        self.collect()
        record, _created = self.prepare()
        self.assertEqual(record.state, "prepared", record.failure_code)
        return record, preparation_directory(record)

    def test_real_copy_is_rechecked_without_changing_files_or_authorizing_activation(self):
        from backups.services.import_preparation import inspect_preparation
        record, directory = self.prepared()
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}
        inspected = inspect_preparation(self.operator, record.id)
        self.assertTrue(inspected["integrity_checked"])
        self.assertEqual(inspected["candidate_sha256"], record.candidate_sha256)
        self.assertEqual(inspected["candidate_bytes"], record.candidate_bytes)
        self.assertIsNotNone(inspected["checked_at"])
        for flag in ("activation_allowed", "permissions_revalidated", "live_database_replaced"):
            self.assertIs(inspected[flag], False)
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()})
        record.refresh_from_db()
        self.assertEqual(record.state, "prepared")
        self.product.refresh_from_db()
        self.assertEqual(self.product.sale_price_fcfa, 500)

    def test_same_size_data_change_is_detected_and_preserved_for_investigation(self):
        from backups.services.import_preparation import inspect_preparation
        record, directory = self.prepared()
        candidate = directory / "candidate.sqlite3"
        with closing(sqlite3.connect(candidate)) as db:
            db.execute("UPDATE core_product SET sale_price_fcfa=900 WHERE id=?", [self.product.id.hex])
            db.commit()
        self.assertEqual(candidate.stat().st_size, record.candidate_bytes)
        with self.assertRaisesMessage(RestoreImportError, "restore_preparation_receipt_invalid"):
            inspect_preparation(self.operator, record.id)
        with closing(sqlite3.connect(candidate)) as db:
            self.assertEqual(db.execute("SELECT sale_price_fcfa FROM core_product WHERE id=?", [self.product.id.hex]).fetchone()[0], 900)
        record.refresh_from_db()
        self.assertEqual(record.state, "prepared")
        self.product.refresh_from_db()
        self.assertEqual(self.product.sale_price_fcfa, 500)

    def test_changed_receipt_is_refused_and_current_technical_rights_are_required(self):
        from backups.services.import_preparation import inspect_preparation
        from identity.models import User
        record, directory = self.prepared()
        receipt = directory / "prepare_manifest.json"
        receipt.write_text('{"activation_allowed": true}', encoding="utf-8")
        with self.assertRaisesMessage(RestoreImportError, "restore_preparation_receipt_invalid"):
            inspect_preparation(self.operator, record.id)
        with self.assertRaises(PermissionDenied):
            inspect_preparation(self.owner, record.id)
        User.objects.filter(id=self.operator.id).update(is_superuser=False)
        with self.assertRaises(PermissionDenied):
            inspect_preparation(self.operator, record.id)


class PreparedCopyInspectionAdministrationTests(TransactionTestCase):
    setUp = fixtures.ImportedArchivePreparationTests.setUp
    collect = fixtures.ImportedArchivePreparationTests.collect
    prepare = fixtures.ImportedArchivePreparationTests.prepare
    prepared = PreparedCopyInspectionTests.prepared

    def test_explicit_csrf_checked_inspection_reports_integrity_and_detects_later_tampering(self):
        record, directory = self.prepared()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.operator)
        url = reverse("admin:backups_importpreparation_result", args=[record.id])
        self.assertContains(client.get(url, HTTP_ACCEPT_LANGUAGE="en"), "Check private copy integrity")
        self.assertEqual(client.post(url, {}).status_code, 403)
        body = {"csrfmiddlewaretoken": client.cookies["csrftoken"].value}
        page = client.post(url, body, HTTP_ACCEPT_LANGUAGE="fr")
        self.assertContains(page, "Intégrité de la copie vérifiée")
        self.assertNotContains(page, "Ceci ne revérifie pas son intégrité")
        self.assertContains(page, "Cette copie ne peut pas encore être activée.")
        self.assertNotContains(page, self.key.decode())
        self.assertNotContains(page, 'name="activate"')
        candidate = directory / "candidate.sqlite3"
        with closing(sqlite3.connect(candidate)) as db:
            db.execute("UPDATE core_product SET sale_price_fcfa=900")
            db.commit()
        page = client.post(url, body, HTTP_ACCEPT_LANGUAGE="en")
        self.assertContains(page, "The private copy integrity check failed.")
        self.assertNotContains(page, "Private copy integrity checked")
        self.assertNotContains(page, "The private copy is missing or its size has changed.")
        self.assertTrue(candidate.is_file())

    def test_inspection_requires_current_technical_role_and_a_small_body_without_files(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        record, _directory = self.prepared()
        url = reverse("admin:backups_importpreparation_result", args=[record.id])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.generic("POST", url, data=b"", CONTENT_LENGTH="999999999").status_code, 403)
        self.client.force_login(self.operator)
        self.assertEqual(self.client.generic("POST", url, data=b"", CONTENT_LENGTH="65537").status_code, 400)
        response = self.client.post(url, {"part": SimpleUploadedFile("unexpected", b"cipher")}, HTTP_ACCEPT_LANGUAGE="en")
        self.assertContains(response, "The private copy integrity check failed.")
        self.assertNotContains(response, "Private copy integrity checked")
