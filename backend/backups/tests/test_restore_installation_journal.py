"""A durable restore intent must survive interruption and exclude other controls."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import uuid

from django.test import SimpleTestCase

from backups.services.restore_gate import (
    initialize_gate, read_gate, enter_maintenance, abort_maintenance,
    hold_control, acquire_recovery_request, RestoreGateError,
)


class RestoreInstallationJournalTests(SimpleTestCase):
    def setUp(self):
        self.directory=TemporaryDirectory(prefix="restore-install-journal-")
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.database=self.root/"business.sqlite3"
        self.gate=self.root/"gate.sqlite3"
        with closing(sqlite3.connect(self.database)) as db,db:
            db.execute("CREATE TABLE business(value INTEGER)")
            db.execute("INSERT INTO business VALUES(500)")
        self.actor=uuid.uuid4()
        self.operation=uuid.uuid4()
        initialize_gate(self.gate,self.database,operation_id=uuid.uuid4(),requested_by=self.actor,reason="Fixture")
        self.plan={"candidate_sha256":"1"*64,"candidate_bytes":4096,"rollback_directory":str(self.root/"rollback")}

    def enter(self):
        return enter_maintenance(self.gate,operation_id=uuid.uuid4(),requested_by=self.actor,
            reason="Incident",expected_version=1)

    def control(self,**overrides):
        from backups.services.installation_journal import installation_control
        return installation_control(self.gate,self.database,operation_id=self.operation,requested_by=self.actor,
            reason="Installer la copie contrôlée",expected_version=2,plan=self.plan,**overrides)

    def test_new_gate_supports_durable_installation_records(self):
        with closing(sqlite3.connect(self.gate)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gate_installations").fetchone()[0],0)

    def test_completion_identifier_collision_is_rejected_before_reserving_installation(self):
        enter_maintenance(self.gate,operation_id=uuid.uuid5(self.operation,"installed"),requested_by=self.actor,
            reason="Earlier control operation",expected_version=1)
        with self.assertRaises(RestoreGateError):
            with self.control():
                pass
        self.assertEqual(read_gate(self.gate)["version"],2)
        self.assertNotIn("installation",read_gate(self.gate))

    def test_interrupted_intent_persists_and_prevents_abort_control_and_recovery_reads(self):
        self.enter()
        with self.assertRaisesRegex(RuntimeError,"fixture interruption"):
            with self.control() as installation:
                self.assertEqual(installation.state,"reserved")
                raise RuntimeError("fixture interruption")
        state=read_gate(self.gate)
        self.assertEqual((state["mode"],state["version"]),("maintenance",3))
        self.assertEqual(state["installation"],{"operation_id":str(self.operation),"state":"reserved"})
        with self.assertRaises(RestoreGateError):
            abort_maintenance(self.gate,operation_id=uuid.uuid4(),requested_by=self.actor,
                reason="Pas d’abandon implicite",expected_version=3)
        with self.assertRaises(RestoreGateError):
            with hold_control(self.gate,self.database,required_mode="maintenance",expected_version=3):
                self.fail("Another controller entered an unfinished installation")
        with self.assertRaises(RestoreGateError):
            acquire_recovery_request(self.gate,self.database,expected_version=3)
        self.assertEqual(read_gate(self.gate)["mode"],"maintenance")

    def test_resume_and_receipt_replay_never_repeat_the_reservation_or_open_the_gate(self):
        self.enter()
        with self.control():
            pass
        with self.control() as installation:
            receipt=installation.complete({"candidate_sha256":"1"*64,"activation_allowed":False})
        with self.control() as installation:
            self.assertEqual(installation.state,"installed")
            self.assertEqual(installation.receipt,receipt)
        state=read_gate(self.gate)
        self.assertEqual((state["mode"],state["version"]),("maintenance",3))
        with hold_control(self.gate,self.database,required_mode="maintenance",expected_version=3):
            pass
        # Installation is distinct from an unchanged-source maintenance abort.
        with self.assertRaises(RestoreGateError):
            abort_maintenance(self.gate,operation_id=uuid.uuid4(),requested_by=self.actor,
                reason="Activation dédiée requise",expected_version=3)
        with closing(sqlite3.connect(self.gate)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gate_installations").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM gate_events").fetchone()[0],4)

    def test_conflicting_plan_or_another_operation_cannot_take_over_a_pending_installation(self):
        from backups.services.installation_journal import installation_control
        self.enter()
        with self.control():
            pass
        for operation,plan in ((self.operation,{**self.plan,"candidate_sha256":"2"*64}),
                               (uuid.uuid4(),self.plan)):
            with self.subTest(operation=operation),self.assertRaises(RestoreGateError):
                with installation_control(self.gate,self.database,operation_id=operation,requested_by=self.actor,
                        reason="Installer la copie contrôlée",expected_version=2,plan=plan):
                    self.fail("A pending installation changed its request")

    def test_legacy_gate_requires_explicit_upgrade_and_preserves_history_and_database(self):
        from backups.services.restore_gate import upgrade_gate
        # Reproduce the pre-installation format, without data from a future restore.
        with closing(sqlite3.connect(self.gate)) as db,db:
            db.execute("DROP TABLE IF EXISTS gate_installations")
            db.execute("PRAGMA user_version=1")
            original=db.execute("SELECT operation_id,receipt FROM gate_events").fetchall()
        before=self.database.read_bytes()
        self.enter()
        with self.assertRaises(RestoreGateError):
            with self.control():
                self.fail("Legacy gate silently accepted installation")
        operation=uuid.uuid4()
        receipt=upgrade_gate(self.gate,self.database,operation_id=operation,requested_by=self.actor,
            reason="Journal durable",expected_version=2)
        self.assertEqual((receipt["mode"],receipt["version"]),("maintenance",3))
        replay=upgrade_gate(self.gate,self.database,operation_id=operation,requested_by=self.actor,
            reason="Journal durable",expected_version=2)
        self.assertEqual(receipt,replay)
        self.assertEqual(before,self.database.read_bytes())
        with closing(sqlite3.connect(self.gate)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT receipt FROM gate_events WHERE operation_id=?",[original[0][0]]).fetchone()[0],original[0][1])

    def test_legacy_upgrade_is_available_without_django_in_the_hosting_console(self):
        with closing(sqlite3.connect(self.gate)) as db,db:
            db.execute("DROP TABLE gate_installations")
            db.execute("PRAGMA user_version=1")
        self.enter()
        script=Path(__file__).resolve().parents[3]/"tools/restore_maintenance.py"
        command=[sys.executable,"-I","-X","utf8",str(script),"upgrade","--gate",str(self.gate),
            "--database",str(self.database),"--operation-id",str(self.operation),"--requested-by",str(self.actor),
            "--reason","Journal durable","--expected-version","2"]
        completed=subprocess.run(command,capture_output=True,text=True,timeout=20)
        self.assertEqual(completed.returncode,0,completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["version"],3)
        self.assertEqual(read_gate(self.gate)["gate_format"],2)
