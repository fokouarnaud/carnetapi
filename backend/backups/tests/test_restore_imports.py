"""US-43: authenticated, resumable encrypted collection with current technical rights."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch
import uuid

from cryptography.fernet import Fernet
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import PermissionDenied
from backups.models import RestoreImport, RestoreImportPart
from backups.services.archive import build_archive, inspect_part
from common.services.errors import OperationConflict
from identity.models import User


class RestoreCollectionTests(TransactionTestCase):
    def setUp(self):
        self.operator = User.objects.create_superuser(username="restore-collector", password=None)
        self.owner = User.objects.create_user(username="restore-business", is_staff=True)
        self.directory = TemporaryDirectory(prefix="restore-collection-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.storage = self.root / "stored"
        self.options = override_settings(BACKUP_ROOT=self.storage)
        self.options.enable()
        self.addCleanup(self.options.disable)
        self.key = Fernet.generate_key()
        self.batch_id = uuid.uuid4()
        snapshot = self.root / "source.sqlite3"
        with closing(sqlite3.connect(snapshot)) as database:
            database.execute("CREATE TABLE django_migrations (app TEXT, name TEXT)")
            database.execute("INSERT INTO django_migrations VALUES ('core','0001_initial')")
            database.execute("CREATE TABLE contents (photo BLOB)")
            database.execute("INSERT INTO contents VALUES (?)", [os.urandom(90000)])
            database.commit()
        self.archive = build_archive(snapshot, self.root / "parts", self.key,
            backup_id=uuid.uuid4(), key_id="collection-test", max_part_bytes=32768)
        self.paths = [self.root / "parts" / part["filename"] for part in self.archive.parts]
        self.assertGreater(len(self.paths), 2)

    def collect(self, index, **values):
        from backups.services.restore_imports import collect_part
        return collect_part(**{"actor": self.operator, "batch_id": self.batch_id,
            "reason": "Reprise contrôlée", "upload": SimpleUploadedFile("untrusted-name.axbkp", self.paths[index].read_bytes()),
            "key": self.key, **values})

    def test_out_of_order_upload_replay_and_missing_parts_are_explicit_without_activation(self):
        from backups.services.restore_imports import import_progress, imported_part_path
        batch, part, created = self.collect(1)
        self.assertTrue(created)
        progress = import_progress(self.operator, batch.id)
        self.assertEqual(progress["received"], [2])
        self.assertEqual(progress["missing"], [1, *range(3, len(self.paths)+1)])
        self.assertEqual(progress["received_count"], 1)
        self.assertFalse(progress["collection_complete"])
        replay_batch, replay, created = self.collect(1)
        self.assertFalse(created)
        self.assertEqual((replay_batch.id, replay.id), (batch.id, part.id))
        self.assertEqual(imported_part_path(batch, part).read_bytes(), self.paths[1].read_bytes())
        for index in reversed(range(len(self.paths))):
            self.collect(index)
        progress = import_progress(self.operator, batch.id)
        self.assertTrue(progress["collection_complete"])
        self.assertEqual(progress["missing"], [])
        self.assertFalse(progress["archive_verified"])
        self.assertFalse(progress["activation_allowed"])
        self.assertEqual(RestoreImport.objects.count(), 1)
        self.assertEqual(RestoreImportPart.objects.count(), len(self.paths))
        self.assertNotIn(self.key.decode(), json.dumps(batch.header))
        self.assertFalse(list(self.storage.rglob("*.sqlite3")))

    def test_owner_cannot_read_or_upload_and_is_rejected_before_reading_the_body(self):
        from backups.services.restore_imports import collect_part, import_progress
        class UnreadableUpload:
            def chunks(self, *args, **kwargs):
                raise AssertionError("unauthorized file read")
        with self.assertRaises(PermissionDenied):
            collect_part(self.owner, self.batch_id, "Refus", UnreadableUpload(), self.key)
        self.assertFalse(self.storage.exists())
        batch, _, _ = self.collect(0)
        with self.assertRaises(PermissionDenied):
            import_progress(self.owner, batch.id)

    def test_wrong_key_and_mixed_archive_do_not_create_or_change_inventory(self):
        from backups.services.restore_imports import RestoreImportError, imported_part_path
        with self.assertRaisesMessage(RestoreImportError, "backup_authentication_failed"):
            self.collect(0, key=Fernet.generate_key())
        self.assertFalse(RestoreImport.objects.exists())
        batch, part, _ = self.collect(0)
        original = imported_part_path(batch, part).read_bytes()
        other = build_archive(self.root / "source.sqlite3", self.root / "other", self.key,
            backup_id=uuid.uuid4(), key_id="collection-test", max_part_bytes=32768)
        foreign = self.root / "other" / other.parts[1]["filename"]
        with self.assertRaises(OperationConflict):
            self.collect(1, upload=SimpleUploadedFile("foreign.axbkp", foreign.read_bytes()))
        self.assertEqual(RestoreImportPart.objects.count(), 1)
        self.assertEqual(imported_part_path(batch, part).read_bytes(), original)
        self.assertFalse(list(self.storage.glob("part-upload-*")))

    def test_revocation_during_authentication_rolls_back_metadata_and_published_files(self):
        def revoke(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            result = inspect_part(*args, **kwargs)
            User.objects.filter(id=self.operator.id).update(is_superuser=False)
            return result
        with patch("backups.services.restore_imports.inspect_part", side_effect=revoke):
            with self.assertRaises(PermissionDenied):
                self.collect(0)
        self.assertFalse(RestoreImport.objects.exists())
        self.assertFalse(list(self.storage.rglob("*.axbkp")))

    def test_unknown_file_at_the_generated_destination_is_preserved_on_conflict(self):
        folder = self.storage / "restore-imports" / str(self.batch_id)
        folder.mkdir(parents=True)
        occupied = folder / "part-00001.axbkp"
        occupied.write_bytes(b"unrelated existing file")
        with self.assertRaises(OperationConflict):
            self.collect(0)
        self.assertEqual(occupied.read_bytes(), b"unrelated existing file")
        self.assertFalse(RestoreImport.objects.exists())

    def test_inventory_paginates_both_received_and_missing_parts_with_global_totals(self):
        from backups.services.restore_imports import import_progress, RestoreImportError
        batch = RestoreImport.objects.create(requested_by=self.operator, reason="Pagination",
            backup_id=uuid.uuid4(), part_count=101, snapshot_bytes=8192, header_sha256="a"*64, header={})
        RestoreImportPart.objects.bulk_create([RestoreImportPart(batch=batch, uploaded_by=self.operator,
            number=number, size_bytes=100, sha256="b"*64) for number in range(2,102,2)])
        first = import_progress(self.operator, batch.id)
        self.assertEqual(first["received"], list(range(2,102,2)))
        self.assertEqual(first["missing"], list(range(1,100,2)))
        second = import_progress(self.operator, batch.id, page=2)
        self.assertEqual(second["missing"], [101])
        self.assertEqual(second["received"], [])
        self.assertEqual((second["received_count"], second["missing_count"]), (50,51))
        with self.assertRaisesMessage(RestoreImportError, "restore_import_page_invalid"):
            import_progress(self.operator, batch.id, page=4)

    def test_missing_registered_file_is_repaired_without_a_second_receipt(self):
        from backups.services.restore_imports import imported_part_path
        batch, part, _created = self.collect(0)
        path = imported_part_path(batch, part)
        path.unlink()
        _, replay, created = self.collect(0)
        self.assertFalse(created)
        self.assertEqual(replay.id, part.id)
        self.assertEqual(path.read_bytes(), self.paths[0].read_bytes())
        self.assertEqual(RestoreImportPart.objects.count(), 1)

    def test_different_encryption_of_the_same_part_cannot_overwrite_a_collected_file(self):
        import base64
        from backups.services.archive import MAGIC
        from backups.services.restore_imports import imported_part_path
        batch, part, _created = self.collect(0)
        before = imported_part_path(batch, part).read_bytes()
        plain = Fernet(self.key).decrypt(base64.urlsafe_b64encode(before[len(MAGIC):]))
        replacement = MAGIC+base64.urlsafe_b64decode(Fernet(self.key).encrypt(plain))
        with self.assertRaises(OperationConflict):
            self.collect(0, upload=SimpleUploadedFile("replacement", replacement))
        self.assertEqual(imported_part_path(batch, part).read_bytes(), before)
        self.assertEqual(RestoreImportPart.objects.count(), 1)

    def test_failure_after_file_publication_rolls_back_only_our_file_and_receipt(self):
        from backups.services.jobs import authorize
        calls = []
        def permission_check(*args, **kwargs):
            calls.append(True)
            if len(calls) == 3:
                raise PermissionDenied()
            return authorize(*args, **kwargs)
        with patch("backups.services.restore_imports.authorize", side_effect=permission_check):
            with self.assertRaises(PermissionDenied):
                self.collect(0)
        self.assertEqual(len(calls), 3)
        self.assertFalse(RestoreImport.objects.exists())
        self.assertFalse(list(self.storage.rglob("*.axbkp")))

    def test_unknown_schema_and_oversized_direct_upload_leave_no_inventory(self):
        from backups.services.restore_imports import RestoreImportError
        with self.assertRaisesMessage(RestoreImportError, "encrypted_part_limit"):
            self.collect(0, upload=SimpleUploadedFile("too-large", b"x"*15000001))
        with closing(sqlite3.connect(self.root / "source.sqlite3")) as database:
            database.execute("UPDATE django_migrations SET name='not_an_installed_migration'")
            database.commit()
        foreign = build_archive(self.root / "source.sqlite3", self.root / "future", self.key,
            backup_id=uuid.uuid4(), key_id="collection-test", max_part_bytes=32768)
        path = self.root / "future" / foreign.parts[0]["filename"]
        with self.assertRaisesMessage(RestoreImportError, "backup_schema_incompatible"):
            self.collect(0, upload=SimpleUploadedFile("unknown-schema", path.read_bytes()))
        self.assertFalse(RestoreImport.objects.exists())
        self.assertFalse(list(self.storage.rglob("*.axbkp")))

    def test_unexpected_error_report_masks_key_in_the_collector_and_its_callees(self):
        from django.test import RequestFactory
        from django.views.debug import SafeExceptionReporterFilter
        request = RequestFactory().post("/restore-error-check/")
        def unexpected(path, key):
            raise RuntimeError("unexpected-import-failure")
        with patch("backups.services.restore_imports.inspect_part", side_effect=unexpected):
            try:
                self.collect(0)
            except RuntimeError as failure:
                frames = []
                trace = failure.__traceback__
                while trace is not None:
                    if (trace.tb_frame.f_code.co_filename.endswith("restore_imports.py")
                            or trace.tb_frame.f_code.co_name == "unexpected"):
                        frames.append(trace.tb_frame)
                    trace = trace.tb_next
                self.assertGreaterEqual(len(frames), 2)
                filtering = SafeExceptionReporterFilter()
                for frame in frames:
                    variables = filtering.get_traceback_frame_variables(request, frame)
                    self.assertFalse(self.key.decode() in repr(variables), "key visible in traceback variables")
            else:
                self.fail("Fault injection did not exercise the exception path")
