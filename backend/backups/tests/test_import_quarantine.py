"""US-43: durable, authorized quarantine commands wrap the independent copier."""
import hashlib
import sqlite3
from contextlib import closing
from unittest.mock import patch
import uuid
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from backups.models import ImportQuarantine
from backups.services.restore_imports import RestoreImportError
from backups.tests import test_import_preparation as fixtures
from common.services.errors import OperationConflict
from identity.models import User


class ImportedQuarantineTests(TransactionTestCase):
    setUp = fixtures.ImportedArchivePreparationTests.setUp
    collect = fixtures.ImportedArchivePreparationTests.collect
    prepare = fixtures.ImportedArchivePreparationTests.prepare

    def prepared_source(self):
        self.collect()
        record, _created = self.prepare()
        self.assertEqual(record.state, "prepared", record.failure_code)
        return record

    def quarantine(self, source, **values):
        from backups.services.import_quarantine import quarantine_import
        return quarantine_import(**{"actor":self.operator,"preparation_id":source.id,
            "operation_id":self.operation_id,"reason":"Suspendre les anciens accès", **values})

    def test_real_copy_is_registered_outside_transactions_and_replay_has_no_second_effect(self):
        from backups.services.import_preparation import preparation_directory
        from backups.services.import_quarantine import quarantine_directory
        from backups.services.restore_quarantine import quarantine_restore
        source = self.prepared_source()
        before = hashlib.sha256((preparation_directory(source)/"candidate.sqlite3").read_bytes()).hexdigest()
        def outside(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            return quarantine_restore(*args, **kwargs)
        with patch("backups.services.import_quarantine.quarantine_restore", side_effect=outside):
            record, created = self.quarantine(source)
        self.assertTrue(created)
        self.assertEqual(record.state, "quarantined", record.failure_code)
        candidate = quarantine_directory(record)/"candidate.sqlite3"
        self.assertEqual(record.candidate_sha256, hashlib.sha256(candidate.read_bytes()).hexdigest())
        with closing(sqlite3.connect(candidate)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM core_user WHERE is_active!=0 OR is_staff!=0 OR is_superuser!=0").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT sale_price_fcfa FROM core_product WHERE id=?",[self.product.id.hex]).fetchone()[0], 500)
        with patch("backups.services.import_quarantine.quarantine_restore", side_effect=AssertionError("second copy")):
            replay, created = self.quarantine(source)
        self.assertFalse(created)
        self.assertEqual(replay.id, record.id)
        self.assertEqual(ImportQuarantine.objects.count(), 1)
        with self.assertRaises(OperationConflict):
            self.quarantine(source, reason="Autre commande")
        self.assertEqual(hashlib.sha256((preparation_directory(source)/"candidate.sqlite3").read_bytes()).hexdigest(), before)
        self.assertTrue(User.objects.get(id=self.operator.id).is_superuser)

    def test_business_role_is_denied_before_any_record_or_copy(self):
        from backups.services.import_quarantine import quarantine_import
        with self.assertRaises(PermissionDenied):
            quarantine_import(self.owner, uuid.uuid4(), uuid.uuid4(), "Refus")
        self.assertFalse(ImportQuarantine.objects.exists())

    def test_changed_source_is_recorded_as_failure_without_destroying_it(self):
        from backups.services.import_quarantine import quarantine_directory
        from backups.services.import_preparation import preparation_directory
        source = self.prepared_source()
        candidate = preparation_directory(source)/"candidate.sqlite3"
        with closing(sqlite3.connect(candidate)) as db:
            db.execute("UPDATE core_product SET sale_price_fcfa=900")
            db.commit()
        record, created = self.quarantine(source)
        self.assertTrue(created)
        self.assertEqual((record.state, record.failure_code), ("failed", "restore_preparation_receipt_invalid"))
        self.assertTrue(candidate.is_file())
        self.assertFalse(quarantine_directory(record).exists())

    def test_pending_quarantine_replays_and_blocks_both_another_quarantine_and_preparation(self):
        from backups.services.import_quarantine import ImportQuarantineBusy
        from backups.services.import_preparation import ImportPreparationBusy
        source = self.prepared_source()
        record = ImportQuarantine.objects.create(preparation=source, operation_id=self.operation_id,
            requested_by=self.operator, reason="Suspendre les anciens accès")
        self.assertEqual(self.quarantine(source), (record, False))
        with self.assertRaises(ImportQuarantineBusy):
            self.quarantine(source, operation_id=uuid.uuid4())
        with self.assertRaises(ImportPreparationBusy):
            self.prepare(operation_id=uuid.uuid4())
        ImportQuarantine.objects.filter(id=record.id).update(state="failed", finished_at=timezone.now(), failure_code="restore_quarantine_failed")
        from backups.models import ImportPreparation
        ImportPreparation.objects.create(batch_id=source.batch_id, requested_by=self.operator,
            operation_id=uuid.uuid4(), reason="Autre préparation enregistrée en cours")
        with self.assertRaises(ImportQuarantineBusy):
            self.quarantine(source, operation_id=uuid.uuid4())

    def test_unknown_error_does_not_persist_private_values(self):
        source = self.prepared_source()
        with patch("backups.services.import_quarantine.quarantine_restore", side_effect=RuntimeError(self.key.decode())):
            record, _created = self.quarantine(source)
        self.assertEqual((record.state, record.failure_code), ("failed", "restore_import_quarantine_failed"))
        self.assertNotIn(self.key.decode(), repr(record.__dict__))

    def test_withdrawn_technical_role_denies_response_but_preserves_completed_copy(self):
        from backups.services.import_quarantine import quarantine_directory
        from backups.services.restore_quarantine import quarantine_restore
        source = self.prepared_source()
        def withdraw(*args, **kwargs):
            result = quarantine_restore(*args, **kwargs)
            User.objects.filter(id=self.operator.id).update(is_superuser=False)
            return result
        with patch("backups.services.import_quarantine.quarantine_restore", side_effect=withdraw):
            with self.assertRaises(PermissionDenied):
                self.quarantine(source)
        record = ImportQuarantine.objects.get()
        self.assertEqual(record.state, "quarantined", record.failure_code)
        self.assertTrue((quarantine_directory(record)/"candidate.sqlite3").is_file())
