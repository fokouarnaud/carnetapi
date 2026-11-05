"""US-43: an external SQLite barrier drains readers before maintenance."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import uuid
from django.test import SimpleTestCase


class RestoreGateTests(SimpleTestCase):
    def test_missing_business_file_is_not_authorized_for_implicit_sqlite_creation(self):
        from backups.services.restore_gate import acquire_request, RestoreGateError
        self.initialize()
        self.database.unlink()
        with self.assertRaises(RestoreGateError):
            acquire_request(self.gate,self.database)
        self.assertFalse(self.database.exists())

    def test_recovery_lease_requires_maintenance_version_and_excludes_replacement_until_closed(self):
        from backups.services.restore_gate import acquire_recovery_request, acquire_request, hold_control, RestoreGateError
        self.initialize()
        with self.assertRaises(RestoreGateError):
            acquire_recovery_request(self.gate,self.database,expected_version=1)
        self.enter()
        with self.assertRaises(RestoreGateError):
            acquire_recovery_request(self.gate,self.database,expected_version=1)
        lease=acquire_recovery_request(self.gate,self.database,expected_version=2)
        try:
            with self.assertRaises(RestoreGateError):
                acquire_request(self.gate,self.database)
            with self.assertRaises(RestoreGateError):
                with hold_control(self.gate,self.database,required_mode="maintenance",expected_version=2):
                    self.fail("A recovery response still owns its read lease")
        finally:
            lease.close()
        with hold_control(self.gate,self.database,required_mode="maintenance",expected_version=2):
            pass

    def setUp(self):
        self.directory = TemporaryDirectory(prefix="restore-gate-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root/"business.sqlite3"
        self.gate = self.root/"gate.sqlite3"
        self.actor = uuid.uuid4()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE business (amount INTEGER NOT NULL)")
            db.execute("INSERT INTO business VALUES (500)")
            db.commit()

    def initialize(self):
        from backups.services.restore_gate import initialize_gate
        return initialize_gate(self.gate,self.database,operation_id=uuid.uuid4(),requested_by=self.actor,reason="Installation initiale")

    def enter(self, **values):
        from backups.services.restore_gate import enter_maintenance
        return enter_maintenance(self.gate,**{"operation_id":uuid.uuid4(),"requested_by":self.actor,
            "reason":"Préparer une restauration","expected_version":1,"timeout":0,**values})

    def test_state_and_audit_are_durable_and_gate_database_is_distinct_from_business(self):
        from backups.services.restore_gate import read_gate, acquire_request, abort_maintenance
        before=hashlib.sha256(self.database.read_bytes()).hexdigest()
        initialized=self.initialize()
        self.assertEqual((initialized["mode"],initialized["version"]),("open",1))
        with acquire_request(self.gate,self.database):
            self.assertEqual(read_gate(self.gate)["mode"],"open")
        entered=self.enter()
        self.assertEqual((entered["mode"],entered["version"]),("maintenance",2))
        reopened=abort_maintenance(self.gate,operation_id=uuid.uuid4(),requested_by=self.actor,
            reason="Abandon avant remplacement",expected_version=2,timeout=0)
        self.assertEqual((reopened["mode"],reopened["version"]),("open",3))
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(),before)
        with closing(sqlite3.connect(self.gate)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gate_events").fetchone()[0],3)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(),[])
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE gate_state SET mode='unknown'")

    def test_live_request_prevents_maintenance_and_closed_gate_refuses_new_requests(self):
        from backups.services.restore_gate import acquire_request, RestoreGateBusy, RestoreGateClosed, read_gate
        self.initialize()
        with acquire_request(self.gate,self.database):
            with self.assertRaises(RestoreGateBusy):
                self.enter()
            self.assertEqual(read_gate(self.gate)["version"],1)
        self.enter()
        with self.assertRaises(RestoreGateClosed):
            acquire_request(self.gate,self.database)

    def test_console_control_holds_the_expected_mode_version_and_database_exclusively(self):
        from backups.services.restore_gate import hold_control, acquire_request, RestoreGateBusy, RestoreGateConflict
        self.initialize()
        with hold_control(self.gate, self.database, required_mode="open", expected_version=1) as state:
            self.assertEqual((state["mode"], state["version"]), ("open", 1))
            with self.assertRaises(RestoreGateBusy):
                acquire_request(self.gate, self.database)
        with self.assertRaises(RestoreGateConflict):
            with hold_control(self.gate, self.database, required_mode="open", expected_version=2):
                self.fail("wrong version cannot enter")
        self.enter()
        with self.assertRaises(RestoreGateConflict):
            with hold_control(self.gate, self.database, required_mode="open", expected_version=2):
                self.fail("provisioning cannot enter maintenance")
        with hold_control(self.gate, self.database, required_mode="maintenance", expected_version=2) as state:
            self.assertEqual(state["mode"], "maintenance")

    def test_replay_is_a_historical_receipt_and_changed_requests_or_versions_are_refused(self):
        from backups.services.restore_gate import read_gate, abort_maintenance, RestoreGateConflict
        self.initialize()
        operation=uuid.uuid4()
        receipt=self.enter(operation_id=operation)
        self.assertEqual(self.enter(operation_id=operation),receipt)
        with self.assertRaises(RestoreGateConflict):
            self.enter(operation_id=operation,reason="Autre commande")
        abort_maintenance(self.gate,operation_id=uuid.uuid4(),requested_by=self.actor,reason="Abandon",expected_version=2,timeout=0)
        self.assertEqual(self.enter(operation_id=operation),receipt)
        self.assertEqual(read_gate(self.gate)["version"],3)
        with self.assertRaises(RestoreGateConflict):
            self.enter()

    def test_aborting_cannot_reopen_after_the_business_database_changed(self):
        from backups.services.restore_gate import abort_maintenance, read_gate, RestoreGateError
        self.initialize()
        self.enter()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE business SET amount=900")
            db.commit()
        with self.assertRaisesMessage(RestoreGateError,"restore_gate_database_changed"):
            abort_maintenance(self.gate,operation_id=uuid.uuid4(),requested_by=self.actor,reason="Abandon tardif",expected_version=2,timeout=0)
        self.assertEqual((read_gate(self.gate)["mode"],read_gate(self.gate)["version"]),("maintenance",2))

    def test_missing_gate_wrong_database_and_existing_output_fail_without_overwrite(self):
        from backups.services.restore_gate import acquire_request, initialize_gate, RestoreGateError
        with self.assertRaises(RestoreGateError):
            acquire_request(self.gate,self.database)
        self.assertFalse(self.gate.exists())
        self.initialize()
        with self.assertRaises(RestoreGateError):
            acquire_request(self.gate,self.root/"wrong.sqlite3")
        before=self.gate.read_bytes()
        with self.assertRaises(RestoreGateError):
            initialize_gate(self.gate,self.database,operation_id=uuid.uuid4(),requested_by=self.actor,reason="Ne pas écraser")
        self.assertEqual(self.gate.read_bytes(),before)

    def test_a_request_in_another_process_holds_the_barrier_until_it_finishes(self):
        from backups.services.restore_gate import RestoreGateBusy
        self.initialize()
        ready,release=self.root/"ready",self.root/"release"
        backend=Path(__file__).resolve().parents[2]
        code='''
import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from backups.services.restore_gate import acquire_request
with acquire_request(Path(sys.argv[2]),Path(sys.argv[3])):
 Path(sys.argv[4]).write_text('held')
 until=time.monotonic()+10
 while not Path(sys.argv[5]).exists() and time.monotonic()<until: time.sleep(.01)
print('released')
'''
        child=subprocess.Popen([sys.executable,"-I","-X","utf8","-c",code,str(backend),str(self.gate),
            str(self.database),str(ready),str(release)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            until=time.monotonic()+5
            while not ready.exists() and child.poll() is None and time.monotonic()<until:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            with self.assertRaises(RestoreGateBusy):
                self.enter()
            release.write_text("finish")
            stdout,stderr=child.communicate(timeout=10)
            self.assertEqual(child.returncode,0,stderr)
            self.assertEqual(stdout.strip(),"released")
            self.assertEqual(self.enter()["mode"],"maintenance")
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
