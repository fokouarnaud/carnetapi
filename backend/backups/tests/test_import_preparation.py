"""US-43: prepare an imported bundle on a private copy, outside SQL transactions."""
from contextlib import closing
import hashlib
from io import BytesIO
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch
import uuid
from cryptography.fernet import Fernet
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TransactionTestCase, override_settings
from PIL import Image
from rest_framework.exceptions import PermissionDenied
from backups.models import RestoreImport
from backups.services.archive import build_archive
from backups.services.restore_imports import collect_part, RestoreImportError
from common.services.errors import OperationConflict
from catalogue.models import Product, ProductPhoto
from identity.models import User, Shop


class ImportedArchivePreparationTests(TransactionTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="import-preparation-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.options = override_settings(BACKUP_ROOT=self.root / "stored")
        self.options.enable()
        self.addCleanup(self.options.disable)
        self.key = Fernet.generate_key()
        self.operator = User.objects.create_superuser(username="prepare-import", password=None)
        self.owner = User.objects.create_user(username="prepare-owner", is_staff=True)
        shop = Shop.objects.create(owner=self.owner, name="Boutique conservée")
        self.product = Product.objects.create(shop=shop, name="Avant restauration", unit_kind="piece", unit_label="pièce", sale_price_fcfa=500)
        photo = BytesIO()
        Image.new("RGB", (16,16), "blue").save(photo, format="JPEG")
        self.photo = photo.getvalue()
        ProductPhoto.objects.create(product=self.product, data=self.photo, width=16, height=16)
        self.source = self.root / "source.sqlite3"
        connection.ensure_connection()
        with closing(sqlite3.connect(self.source)) as target:
            connection.connection.backup(target)
        self.archive = build_archive(self.source, self.root / "parts", self.key, backup_id=uuid.uuid4(), key_id="preparation-test", max_part_bytes=32768)
        self.paths = [self.root / "parts" / part["filename"] for part in self.archive.parts]
        self.batch_id = uuid.uuid4()
        self.operation_id = uuid.uuid4()

    def collect(self, paths=None):
        for path in self.paths if paths is None else paths:
            collect_part(self.operator, self.batch_id, "Archive à vérifier", SimpleUploadedFile("part", path.read_bytes()), self.key)

    def prepare(self, **values):
        from backups.services.import_preparation import prepare_import
        return prepare_import(**{"actor": self.operator, "batch_id": self.batch_id,
            "operation_id": self.operation_id, "reason": "Vérification complète", "key": self.key, **values})

    def test_real_preparation_checks_copy_preserves_source_and_replays_without_a_second_copy(self):
        from backups.services.import_preparation import preparation_directory
        self.collect()
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        from backups.services.restore_preparation import prepare_restore
        def outside_transaction(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            return prepare_restore(*args, **kwargs)
        with patch("backups.services.import_preparation.prepare_restore", side_effect=outside_transaction):
            prepared, created = self.prepare()
        self.assertTrue(created)
        self.assertEqual(prepared.state, "prepared", prepared.failure_code)
        candidate = preparation_directory(prepared) / "candidate.sqlite3"
        self.assertEqual(prepared.candidate_sha256, hashlib.sha256(candidate.read_bytes()).hexdigest())
        self.assertTrue(prepared.manifest["schema_audit_checked"])
        self.assertFalse(prepared.manifest["activation_allowed"])
        self.assertFalse(prepared.manifest["permissions_revalidated"])
        self.assertFalse(prepared.manifest["live_database_replaced"])
        with closing(sqlite3.connect(candidate)) as snapshot:
            self.assertEqual(snapshot.execute("SELECT sale_price_fcfa FROM core_product WHERE id=?", [self.product.id.hex]).fetchone()[0], 500)
            self.assertEqual(bytes(snapshot.execute("SELECT data FROM core_productphoto WHERE product_id=?", [self.product.id.hex]).fetchone()[0]), self.photo)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        self.product.refresh_from_db()
        self.assertEqual(self.product.sale_price_fcfa, 500)
        with patch("backups.services.import_preparation.prepare_restore", side_effect=AssertionError("replay must not create another copy")):
            replay, created = self.prepare()
        self.assertFalse(created)
        self.assertEqual(replay.id, prepared.id)
        with self.assertRaises(OperationConflict):
            self.prepare(reason="Autre commande sous le même UUID")

    def test_missing_parts_and_business_role_are_refused_before_any_preparation(self):
        from backups.models import ImportPreparation
        self.collect(self.paths[:1])
        self.assertGreater(len(self.paths), 1)
        with self.assertRaisesMessage(RestoreImportError, "backup_parts_missing"):
            self.prepare()
        with self.assertRaises(PermissionDenied):
            self.prepare(actor=self.owner)
        self.assertFalse(ImportPreparation.objects.exists())

    def test_wrong_key_is_a_durable_failure_with_no_plaintext_and_no_secret(self):
        from backups.services.import_preparation import preparation_directory
        self.collect()
        bad_key = Fernet.generate_key()
        failed, created = self.prepare(key=bad_key)
        self.assertTrue(created)
        self.assertEqual((failed.state, failed.failure_code), ("failed", "backup_authentication_failed"))
        self.assertIsNotNone(failed.finished_at)
        self.assertEqual(failed.manifest, {})
        self.assertFalse((preparation_directory(failed) / "candidate.sqlite3").exists())
        self.assertNotIn(bad_key.decode(), repr(failed.__dict__))

    def test_corrupted_imported_file_fails_without_running_the_migration_worker(self):
        from backups.models import RestoreImportPart
        from backups.services.restore_imports import imported_part_path
        self.collect()
        batch = RestoreImport.objects.get(id=self.batch_id)
        part = RestoreImportPart.objects.filter(batch=batch).first()
        imported_part_path(batch, part).write_bytes(b"corrupt imported cipher")
        with patch("backups.services.import_preparation.prepare_restore", side_effect=AssertionError("corrupt file reached worker")):
            failed, _created = self.prepare()
        self.assertEqual((failed.state, failed.failure_code), ("failed", "restore_import_part_unavailable"))

    def test_running_operation_replays_its_record_and_blocks_a_second_worker(self):
        from backups.models import ImportPreparation
        from backups.services.import_preparation import ImportPreparationBusy
        self.collect()
        first = ImportPreparation.objects.create(batch_id=self.batch_id, operation_id=self.operation_id,
            requested_by=self.operator, reason="Vérification complète")
        with patch("backups.services.import_preparation.prepare_restore", side_effect=AssertionError("second worker")):
            replay, created = self.prepare()
            self.assertEqual(replay.id, first.id)
            self.assertFalse(created)
            with self.assertRaises(ImportPreparationBusy):
                self.prepare(operation_id=uuid.uuid4())
        self.assertEqual(ImportPreparation.objects.count(), 1)

    def test_space_failure_is_recorded_without_consuming_any_imported_file(self):
        from types import SimpleNamespace
        self.collect()
        with patch("backups.services.import_preparation.shutil.disk_usage", return_value=SimpleNamespace(free=0)), \
                patch("backups.services.import_preparation.prepare_restore", side_effect=AssertionError("no space")):
            failed, _created = self.prepare()
        self.assertEqual((failed.state, failed.failure_code), ("failed", "restore_preparation_space_insufficient"))
        self.assertEqual(RestoreImport.objects.get(id=self.batch_id).parts.count(), len(self.paths))

    def test_unknown_worker_exception_does_not_persist_its_message_or_the_key(self):
        self.collect()
        with patch("backups.services.import_preparation.prepare_restore", side_effect=RuntimeError("failure "+self.key.decode())):
            failed, _created = self.prepare()
        self.assertEqual(failed.failure_code, "restore_import_preparation_failed")
        self.assertNotIn(self.key.decode(), repr(failed.__dict__))

    def test_role_withdrawn_during_preparation_denies_response_but_preserves_the_verified_copy(self):
        from backups.models import ImportPreparation
        from backups.services.restore_preparation import prepare_restore
        from backups.services.import_preparation import preparation_directory
        self.collect()
        def withdraw(*args, **kwargs):
            result = prepare_restore(*args, **kwargs)
            User.objects.filter(id=self.operator.id).update(is_superuser=False)
            return result
        with patch("backups.services.import_preparation.prepare_restore", side_effect=withdraw):
            with self.assertRaises(PermissionDenied):
                self.prepare()
        record = ImportPreparation.objects.get()
        self.assertEqual(record.state, "prepared", record.failure_code)
        self.assertTrue((preparation_directory(record) / "candidate.sqlite3").is_file())
        self.assertFalse(record.manifest["activation_allowed"])
