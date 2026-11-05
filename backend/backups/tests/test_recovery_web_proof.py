"""The recovery interface trusts a consumed external bootstrap, never an archive role."""
from contextlib import closing
import sqlite3
import uuid
from django.test import TransactionTestCase
from backups.tests import test_technical_recovery as fixtures


class RecoveryWebProofTests(TransactionTestCase):
    setUp=fixtures.TechnicalRecoveryTests.setUp
    provision=fixtures.TechnicalRecoveryTests.provision
    suspend=fixtures.TechnicalRecoveryTests.suspend
    bootstrap=fixtures.TechnicalRecoveryTests.bootstrap

    def identity(self,operation,**values):
        from backups.services.recovery_web import recovery_identity
        return recovery_identity(self.source,self.vault,bootstrap_operation=operation,
            gate_version=values.pop("gate_version",2),backup_root=self.root/"stored",**values)

    def test_external_consumed_bootstrap_binds_current_generation_and_current_technical_role(self):
        from backups.services.restore_gate import RestoreGateError
        issued=self.provision()
        self.suspend()
        operation=uuid.uuid4()
        with self.assertRaises(RestoreGateError):
            self.identity(operation)
        receipt=self.bootstrap(issued.secret,operation_id=operation)
        result=self.identity(operation)
        self.assertEqual(result["user_id"],receipt["user_id"])
        self.assertEqual(result["generation_id"],receipt["restore_generation_id"])
        for options in ({"gate_version":1},):
            with self.subTest(options=options),self.assertRaises(RestoreGateError):
                self.identity(operation,**options)
        with self.assertRaises(RestoreGateError):
            self.identity(uuid.uuid4())
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE core_user SET is_superuser=0 WHERE id=?",[uuid.UUID(receipt["user_id"]).hex])
        with self.assertRaises(RestoreGateError):
            self.identity(operation)

    def test_missing_holds_changed_schema_or_new_generation_cannot_reuse_the_old_bootstrap(self):
        from backups.services.restore_gate import RestoreGateError
        issued=self.provision()
        self.suspend()
        operation=uuid.uuid4()
        self.bootstrap(issued.secret,operation_id=operation)
        original=self.source.read_bytes()
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("DELETE FROM backups_restoreaccessreview")
        with self.assertRaises(RestoreGateError):
            self.identity(operation)
        self.source.write_bytes(original)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("CREATE TABLE unexpected_recovery (value TEXT)")
        with self.assertRaises(RestoreGateError):
            self.identity(operation)
        self.source.write_bytes(original)
        with closing(sqlite3.connect(self.source)) as db, db:
            old=db.execute("SELECT id FROM backups_restoreaccessgeneration WHERE current_slot=1").fetchone()[0]
            following=uuid.uuid4().hex
            db.execute("UPDATE backups_restoreaccessgeneration SET current_slot=NULL")
            db.execute("INSERT INTO backups_restoreaccessgeneration (id,created_at,updated_at,operation_id,backup_id,initiated_by,current_slot) SELECT ?,created_at,updated_at,?,backup_id,initiated_by,1 FROM backups_restoreaccessgeneration WHERE id=?",[following,uuid.uuid4().hex,old])
            db.execute("UPDATE backups_restoreaccessreview SET generation_id=? WHERE generation_id=?",[following,old])
        with self.assertRaises(RestoreGateError):
            self.identity(operation)
