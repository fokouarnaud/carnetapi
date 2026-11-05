"""US-43: consuming an external proof must commit with its database effect."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from tempfile import TemporaryDirectory
import uuid

from django.test import SimpleTestCase


class RecoveryProofTests(SimpleTestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="recovery-proof-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / "business.sqlite3"
        self.vault = self.root / "external" / "proof.sqlite3"
        self.archives = self.root / "archives"
        self.archives.mkdir()
        self.actor = uuid.uuid4()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL)")

    def issue(self, **options):
        from backups.services.recovery_proof import issue_recovery_code
        return issue_recovery_code(self.vault, self.database, self.archives,
            operation_id=uuid.uuid4(), requested_by=self.actor, reason="Préparation avant incident", **options)

    def redeem(self, secret, **options):
        from backups.services.recovery_proof import redeem_recovery_code
        return redeem_recovery_code(self.vault, self.database, secret, **{
            "operation_id": uuid.uuid4(), "requested_by": self.actor, "reason": "Secours contrôlé",
            "request": {"username": "new-technical"}, **options})

    def count_accounts(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]

    def test_atomic_effect_and_historical_replay_never_recreate_an_account(self):
        from backups.services.recovery_proof import RecoveryProofConflict, RecoveryProofUsed
        issued = self.issue()
        operation = uuid.uuid4()
        with self.redeem(issued.secret, operation_id=operation) as proof:
            self.assertFalse(proof.replayed)
            proof.execute("INSERT INTO restored.accounts VALUES (?,?)", (str(uuid.uuid4()), "new-technical"))
            proof.finish({"username": "new-technical", "created": True})
        self.assertEqual(self.count_accounts(), 1)
        with self.redeem(issued.secret, operation_id=operation) as replay:
            self.assertTrue(replay.replayed)
            self.assertEqual(replay.receipt, {"username": "new-technical", "created": True})
            with self.assertRaises(RecoveryProofConflict):
                replay.execute("DELETE FROM restored.accounts")
        with self.assertRaises(RecoveryProofConflict):
            with self.redeem(issued.secret, operation_id=operation, request={"username": "different"}):
                self.fail("different request cannot enter")
        with self.assertRaises(RecoveryProofUsed):
            with self.redeem(issued.secret):
                self.fail("used proof cannot enter")
        self.assertEqual(self.count_accounts(), 1)

    def test_failed_effect_rolls_back_both_files_and_keeps_the_code_usable(self):
        issued = self.issue()
        with self.assertRaisesMessage(RuntimeError, "synthetic failure"):
            with self.redeem(issued.secret) as proof:
                proof.execute("INSERT INTO restored.accounts VALUES ('aborted','aborted')")
                proof.finish({"created": True})
                raise RuntimeError("synthetic failure")
        self.assertEqual(self.count_accounts(), 0)
        with self.redeem(issued.secret) as proof:
            proof.execute("INSERT INTO restored.accounts VALUES ('successful','new-technical')")
            proof.finish({"created": True})
        self.assertEqual(self.count_accounts(), 1)

    def test_restoring_old_business_bytes_does_not_restore_the_consumed_code(self):
        from backups.services.recovery_proof import RecoveryProofUsed
        issued = self.issue()
        old = self.root / "old-business.sqlite3"
        shutil.copyfile(self.database, old)
        with self.redeem(issued.secret) as proof:
            proof.execute("INSERT INTO restored.accounts VALUES ('new','new-technical')")
            proof.finish({"created": True})
        shutil.copyfile(old, self.database)  # Only the synthetic, closed fixture.
        self.assertEqual(self.count_accounts(), 0)
        with self.assertRaises(RecoveryProofUsed):
            with self.redeem(issued.secret):
                self.fail("business rollback must not restore proof")

    def test_wrong_code_and_wrong_database_cannot_consume_the_proof(self):
        from backups.services.recovery_proof import RecoveryProofError
        issued = self.issue()
        with self.assertRaises(RecoveryProofError):
            with self.redeem("invalid-code"):
                self.fail("wrong code cannot enter")
        with self.assertRaises(RecoveryProofError):
            with self.redeem("\ud800"):
                self.fail("invalid Unicode cannot enter")
        other = self.root / "other.sqlite3"
        shutil.copyfile(self.database, other)
        from backups.services.recovery_proof import redeem_recovery_code
        with self.assertRaises(RecoveryProofError):
            with redeem_recovery_code(self.vault, other, issued.secret, operation_id=uuid.uuid4(),
                    requested_by=self.actor, reason="Wrong database", request={}):
                self.fail("wrong database cannot enter")
        with self.redeem(issued.secret) as proof:
            proof.finish({"verified": True})

    def test_provisioning_never_overwrites_or_stores_a_plain_code_in_either_database(self):
        from backups.services.recovery_proof import issue_recovery_code, RecoveryProofError
        for target in (self.database, self.archives / "unsafe.sqlite3"):
            before = self.database.read_bytes()
            with self.assertRaises(RecoveryProofError):
                issue_recovery_code(target, self.database, self.archives, operation_id=uuid.uuid4(),
                    requested_by=self.actor, reason="Invalid location")
            self.assertEqual(self.database.read_bytes(), before)
        issued = self.issue()
        before = self.vault.read_bytes()
        with self.assertRaises(RecoveryProofError):
            self.issue()
        self.assertEqual(self.vault.read_bytes(), before)
        self.assertFalse(issued.secret in repr(issued))
        with self.redeem(issued.secret) as proof:
            proof.finish({"verified": True})
        for path in (self.database, self.vault):
            self.assertFalse(issued.secret.encode() in path.read_bytes())
        with closing(sqlite3.connect(self.vault)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recovery_events").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE recovery_proof SET state='unknown'")

    def test_missing_finish_secret_payload_and_early_commit_are_refused_without_effect(self):
        from backups.services.recovery_proof import RecoveryProofError
        issued = self.issue()
        with self.assertRaises(RecoveryProofError):
            with self.redeem(issued.secret) as proof:
                proof.execute("INSERT INTO restored.accounts VALUES ('missing','missing')")
        self.assertEqual(self.count_accounts(), 0)
        with self.assertRaises(RecoveryProofError):
            with self.redeem(issued.secret) as proof:
                proof.execute("INSERT INTO restored.accounts VALUES ('commit','commit')")
                proof.execute("COMMIT")
        self.assertEqual(self.count_accounts(), 0)
        with self.assertRaises(RecoveryProofError):
            with self.redeem(issued.secret) as proof:
                proof.finish({"secret": issued.secret})
        with self.redeem(issued.secret) as proof:
            proof.execute("INSERT INTO restored.accounts VALUES ('final','new-technical')")
            proof.finish({"created": True})
        self.assertEqual(self.count_accounts(), 1)

    def test_wal_target_is_refused_without_modifying_either_file(self):
        from backups.services.recovery_proof import RecoveryProofError
        issued = self.issue()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("PRAGMA journal_mode=WAL")
        before = hashlib.sha256(self.vault.read_bytes()).hexdigest()
        with self.assertRaises(RecoveryProofError):
            with self.redeem(issued.secret):
                self.fail("WAL is not atomic across attached files")
        self.assertEqual(hashlib.sha256(self.vault.read_bytes()).hexdigest(), before)
        self.assertEqual(self.count_accounts(), 0)

    def test_missing_vault_and_protocol_writes_cannot_create_or_consume_a_proof(self):
        from backups.services.recovery_proof import RecoveryProofError
        with self.assertRaises(RecoveryProofError):
            with self.redeem("invalid-code"):
                self.fail("missing vault cannot authorize")
        self.assertFalse(self.vault.exists())
        issued = self.issue()
        with self.assertRaises(RecoveryProofError):
            with self.redeem(issued.secret) as proof:
                proof.execute("UPDATE main.recovery_proof SET state='consumed'")
        with self.redeem(issued.secret) as proof:
            proof.finish({"verified": True})

    def child(self, issued, *, pause=False):
        backend = Path(__file__).resolve().parents[2]
        code = '''
import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from backups.services.recovery_proof import redeem_recovery_code,RecoveryProofUsed
data=json.loads(sys.stdin.readline())
try:
 with redeem_recovery_code(data['vault'],data['database'],data['secret'],operation_id=data['operation'],
  requested_by=data['actor'],reason='Child process',request={'username':data['operation']}) as proof:
  proof.execute('INSERT INTO restored.accounts VALUES (?,?)',(data['operation'],data['operation']))
  proof.finish({'created':True})
  if data['pause']:
   Path(data['ready']).write_text('held')
   time.sleep(10)
 print('consumed')
except RecoveryProofUsed:
 print('used')
'''
        child = subprocess.Popen([sys.executable, "-I", "-X", "utf8", "-c", code, str(backend)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        data = {"vault": str(self.vault), "database": str(self.database), "secret": issued.secret,
                "operation": str(uuid.uuid4()), "actor": str(self.actor), "pause": pause,
                "ready": str(self.root / "child-ready")}
        child.stdin.write(json.dumps(data) + "\n")
        child.stdin.flush()
        return child

    def test_two_processes_can_consume_only_once(self):
        issued = self.issue()
        children = [self.child(issued), self.child(issued)]
        try:
            outcomes = []
            for child in children:
                stdout, stderr = child.communicate(timeout=15)
                self.assertEqual(child.returncode, 0, "Recovery child failed")
                self.assertFalse(bool(stderr), "Recovery child emitted a diagnostic")
                outcomes.append(stdout.strip())
            self.assertEqual(sorted(outcomes), ["consumed", "used"])
            self.assertEqual(self.count_accounts(), 1)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)

    def test_killing_the_writer_before_commit_recovers_both_files_without_burning_the_code(self):
        issued = self.issue()
        child = self.child(issued, pause=True)
        try:
            until = time.monotonic() + 5
            while not (self.root / "child-ready").exists() and child.poll() is None and time.monotonic() < until:
                time.sleep(.01)
            self.assertTrue((self.root / "child-ready").exists(), "Recovery child did not acquire the transaction")
            child.kill()
            child.communicate(timeout=5)
            self.assertEqual(self.count_accounts(), 0)
            with self.redeem(issued.secret) as proof:
                proof.execute("INSERT INTO restored.accounts VALUES ('after-crash','new-technical')")
                proof.finish({"created": True})
            self.assertEqual(self.count_accounts(), 1)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
