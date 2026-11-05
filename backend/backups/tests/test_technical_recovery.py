"""US-43: extra proof creates a new technical identity, never archived rights."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid

from django.contrib.auth.hashers import check_password
from django.test import TransactionTestCase

from backups.services.recovery_proof import RecoveryProofError
from backups.services.restore_gate import initialize_gate, enter_maintenance, read_gate, acquire_request, RestoreGateClosed
from backups.services.restore_quarantine import _suspend
from backups.tests import test_import_preparation as fixtures


class TechnicalRecoveryTests(TransactionTestCase):
    def test_current_generation_with_an_old_confirmed_decision_cannot_bootstrap_as_fresh_quarantine(self):
        issued=self.provision()
        self.suspend()
        with closing(sqlite3.connect(self.source)) as db, db:
            review=db.execute("SELECT id FROM backups_restoreaccessreview LIMIT 1").fetchone()[0]
            db.execute("INSERT INTO backups_restoreaccessdecision (id,created_at,updated_at,review_id,operation_id,requested_by_id,previous_version,version,state,reason,evidence_ref) VALUES (?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,?,?,?,0,1,'confirmed','Ancien contrôle','')",[uuid.uuid4().hex,review,uuid.uuid4().hex,self.operator.id.hex])
        with self.assertRaises(RecoveryProofError):
            self.bootstrap(issued.secret)
        with closing(sqlite3.connect(self.vault)) as db:
            self.assertEqual(db.execute("SELECT state FROM recovery_proof").fetchone()[0],"available")

    def test_bootstrap_receipt_binds_the_actual_generation_and_requires_all_archived_pairs_to_be_held(self):
        issued=self.provision()
        self.suspend()
        with closing(sqlite3.connect(self.source)) as db:
            generation=db.execute("SELECT id FROM backups_restoreaccessgeneration WHERE current_slot=1").fetchone()[0]
        result=self.bootstrap(issued.secret)
        self.assertEqual(result["restore_generation_id"],str(uuid.UUID(generation)))

    def test_missing_scoped_review_refuses_bootstrap_without_consuming_the_external_code(self):
        issued=self.provision()
        self.suspend()
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("DELETE FROM backups_restoreaccessreview")
        with self.assertRaises(RecoveryProofError):
            self.bootstrap(issued.secret)
        with closing(sqlite3.connect(self.vault)) as db:
            self.assertEqual(db.execute("SELECT state FROM recovery_proof").fetchone()[0],"available")

    def setUp(self):
        fixtures.ImportedArchivePreparationTests.setUp(self)
        self.gate, self.vault = self.root / "gate.sqlite3", self.root / "external" / "proof.sqlite3"
        self.password = "Nouveau-compte-technique-82!"
        initialize_gate(self.gate, self.source, operation_id=uuid.uuid4(), requested_by=self.operator.id, reason="Installation")

    def provision(self, **options):
        from backups.services.technical_recovery import provision_technical_recovery
        return provision_technical_recovery(self.gate, options.pop("vault_path", self.vault), self.source,
            self.root / "stored", **{"operation_id": uuid.uuid4(), "requested_by": self.operator.id,
                                    "reason": "Préparation avant panne", "gate_version": 1, **options})

    def suspend(self):
        enter_maintenance(self.gate, operation_id=uuid.uuid4(), requested_by=self.operator.id,
                          reason="Restauration de la fixture privée", expected_version=1)
        _suspend(self.source, datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" "),
            generation_id=uuid.uuid4(), operation_id=uuid.uuid4(), backup_id=uuid.uuid4(), requested_by=self.operator.id)

    def bootstrap(self, secret, **options):
        from backups.services.technical_recovery import bootstrap_technical_recovery
        return bootstrap_technical_recovery(self.gate, self.vault, self.source, secret, **{
            "username": "technical-new", "password": self.password, "language": "fr",
            "operation_id": uuid.uuid4(), "requested_by": self.operator.id,
            "reason": "Accès de secours contrôlé", "gate_version": 2, **options})

    def test_only_current_technical_actor_can_provision_before_maintenance(self):
        with self.assertRaises(RecoveryProofError):
            self.provision(requested_by=self.owner.id)
        self.assertFalse(self.vault.exists())
        issued = self.provision()
        self.assertFalse(issued.secret in repr(issued))
        self.suspend()
        another = self.root / "another-proof.sqlite3"
        with self.assertRaises(RecoveryProofError):
            self.provision(vault_path=another, gate_version=2)
        self.assertFalse(another.exists())

    def test_fresh_technical_account_and_proof_commit_together_with_idempotent_replay(self):
        issued = self.provision()
        self.suspend()
        old_business = self.root / "before-bootstrap.sqlite3"
        shutil.copyfile(self.source, old_business)
        operation = uuid.uuid4()
        before = self.product.sale_price_fcfa
        result = self.bootstrap(issued.secret, operation_id=operation)
        self.assertNotEqual(result["user_id"], str(self.operator.id))
        self.assertFalse(result["permissions_revalidated"])
        self.assertFalse(result["activation_allowed"])
        self.assertEqual(self.bootstrap(issued.secret, operation_id=operation), result)
        with closing(sqlite3.connect(self.source)) as db:
            row = db.execute("SELECT username,password,is_active,is_staff,is_superuser FROM core_user WHERE id=?",
                             (uuid.UUID(result["user_id"]).hex,)).fetchone()
            self.assertEqual((row[0], *row[2:]), ("technical-new", 1, 1, 1))
            self.assertTrue(check_password(self.password, row[1]))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM core_user WHERE is_active=1").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT is_active,is_staff,is_superuser FROM core_user WHERE id=?",
                                        (self.operator.id.hex,)).fetchone(), (0, 0, 0))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM core_membership WHERE is_active=1").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT sale_price_fcfa FROM core_product WHERE id=?", (self.product.id.hex,)).fetchone()[0], before)
        self.assertEqual(read_gate(self.gate)["mode"], "maintenance")
        with self.assertRaises(RestoreGateClosed):
            acquire_request(self.gate, self.source)
        with self.assertRaises(RecoveryProofError):
            self.bootstrap(issued.secret, operation_id=operation, password="Un-autre-mot-de-passe-82!")
        with self.assertRaises(RecoveryProofError):
            self.bootstrap(issued.secret)
        # Authentication is exercised in a fresh Django process on the private file.
        backend = Path(__file__).resolve().parents[2]
        probe = '''
import os,sys,json
sys.path.insert(0,sys.argv[1])
os.environ['DJANGO_SETTINGS_MODULE']='config.test_settings'
os.environ['APP_DB_PATH']=sys.argv[2]
import django
django.setup()
from django.contrib.auth import authenticate
data=json.loads(sys.stdin.read())
user=authenticate(username='technical-new',password=data['password'])
old=authenticate(username='prepare-import',password=data['password'])
print(json.dumps({'new':bool(user and user.is_staff and user.is_superuser),'old':old is not None}))
'''
        probe_result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", probe, str(backend), str(self.source)],
            input=json.dumps({"password": self.password}), text=True, capture_output=True, timeout=30)
        self.assertEqual(probe_result.returncode, 0, "Authentication probe failed")
        self.assertEqual(json.loads(probe_result.stdout), {"new": True, "old": False})
        shutil.copyfile(old_business, self.source)
        with self.assertRaises(RecoveryProofError):
            self.bootstrap(issued.secret, operation_id=operation)
        with closing(sqlite3.connect(self.source)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM core_user WHERE is_active=1").fetchone()[0], 0)

    def test_open_gate_live_rights_bad_schema_and_existing_username_do_not_burn_proof(self):
        issued = self.provision()
        with self.assertRaises(RecoveryProofError):
            self.bootstrap(issued.secret, gate_version=1)
        enter_maintenance(self.gate, operation_id=uuid.uuid4(), requested_by=self.operator.id,
                          reason="Contrôle avant suspension", expected_version=1)
        with self.assertRaises(RecoveryProofError):
            self.bootstrap(issued.secret)  # Existing active accounts are not quarantined.
        _suspend(self.source, datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" "),
            generation_id=uuid.uuid4(), operation_id=uuid.uuid4(), backup_id=uuid.uuid4(), requested_by=self.operator.id)
        with self.assertRaises(RecoveryProofError):
            self.bootstrap(issued.secret, username=self.operator.username)
        with self.assertRaises(RecoveryProofError):
            self.bootstrap(issued.secret, password="123")
        with self.assertRaises(RecoveryProofError):
            self.bootstrap(issued.secret, language="invalid")
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("CREATE TRIGGER unsafe_user AFTER INSERT ON core_user BEGIN UPDATE core_user SET is_active=1; END")
        with self.assertRaises(RecoveryProofError):
            self.bootstrap(issued.secret)
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("DROP TRIGGER unsafe_user")
        result = self.bootstrap(issued.secret)
        self.assertEqual(result["username"], "technical-new")
        self.assertEqual(read_gate(self.gate)["mode"], "maintenance")

    def test_console_bootstrap_ignores_broken_application_settings_and_never_echoes_the_proof(self):
        script = Path(__file__).resolve().parents[3] / "tools" / "technical_recovery.py"
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("APP_SECRET_KEY", "APP_JWT_SIGNING_KEY")}
        environment["DJANGO_SETTINGS_MODULE"] = "intentionally.invalid.settings"
        operation = uuid.uuid4()
        common = ["--gate", str(self.gate), "--vault", str(self.vault), "--database", str(self.source),
                  "--operation-id", str(operation), "--requested-by", str(self.operator.id),
                  "--reason", "Contrôle console privé"]
        refused_vault = self.root / "not-issued.sqlite3"
        refused = subprocess.run([sys.executable, "-I", "-X", "utf8", str(script), "provision",
            "--gate", str(self.gate), "--vault", str(refused_vault), "--database", str(self.source),
            "--backup-root", str(self.root / "stored"), "--operation-id", str(uuid.uuid4()),
            "--requested-by", str(self.operator.id), "--reason", "No interactive delivery", "--gate-version", "1", "--show-code"],
            stdin=subprocess.DEVNULL, text=True, capture_output=True, env=environment, timeout=30)
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse(refused_vault.exists())
        issued = self.provision()
        self.suspend()
        payload = json.dumps({"code": issued.secret, "password": self.password, "password_confirmation": self.password})
        command = [sys.executable, "-I", "-X", "utf8", str(script), "bootstrap", *common,
                   "--gate-version", "2", "--username", "technical-console", "--language", "en", "--stdin-json"]
        invalid = subprocess.run(command, input=json.dumps({"code": issued.secret, "password": self.password,
                "password_confirmation": "different"}), text=True, capture_output=True, env=environment, timeout=30)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertFalse(issued.secret in invalid.stdout or issued.secret in invalid.stderr)
        results = []
        for _ in range(2):
            completed = subprocess.run(command, input=payload, text=True, capture_output=True, env=environment, timeout=45)
            self.assertEqual(completed.returncode, 0, "Console bootstrap failed")
            self.assertFalse(issued.secret in completed.stdout or issued.secret in completed.stderr)
            self.assertFalse(self.password in completed.stdout or self.password in completed.stderr)
            results.append(json.loads(completed.stdout))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["username"], "technical-console")
        self.assertEqual(results[0]["language"], "en")
        self.assertFalse(results[0]["activation_allowed"])
        with closing(sqlite3.connect(self.source)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM core_user WHERE is_active=1").fetchone()[0], 1)
            encoded = db.execute("SELECT password FROM core_user WHERE username='technical-console'").fetchone()[0]
        from django.contrib.auth.hashers import PBKDF2PasswordHasher
        self.assertTrue(PBKDF2PasswordHasher().verify(self.password, encoded))

    def test_provision_console_displays_once_without_persisting_the_code(self):
        script = Path(__file__).resolve().parents[3] / "tools" / "technical_recovery.py"
        helper = '''
import io,json,runpy,sys,sqlite3,hashlib
from pathlib import Path
from contextlib import closing
class Terminal(io.StringIO):
 def isatty(self): return True
out,err=sys.stdout,sys.stderr
stdin,stdout,stderr=Terminal(),Terminal(),Terminal()
sys.stdin,sys.stdout,sys.stderr=stdin,stdout,stderr
main=runpy.run_path(sys.argv[1],run_name='technical_recovery_test')['main']
first=main(sys.argv[2:])
lines=stdout.getvalue().splitlines()
secret=lines[1] if len(lines)==3 else ''
receipt=json.loads(lines[2]) if len(lines)==3 else {}
vault=Path(sys.argv[sys.argv.index('--vault')+1])
before=vault.read_bytes()
stdout.seek(0);stdout.truncate(0)
second=main(sys.argv[2:])
with closing(sqlite3.connect(vault)) as db:
 state=db.execute('SELECT state FROM recovery_proof').fetchone()[0]
sys.stdout,sys.stderr=out,err
print(json.dumps({'first':first,'second':second,'state':state,'strong_code':len(secret)>=40,
 'secret_omitted_from_json':bool(secret and secret not in json.dumps(receipt)),
 'plaintext_absent':bool(secret and secret.encode() not in before),
 'vault_unchanged':before==vault.read_bytes(),'second_stdout_empty':stdout.getvalue()==''}))
'''
        arguments = ["provision", "--gate", str(self.gate), "--vault", str(self.vault),
                     "--database", str(self.source), "--backup-root", str(self.root / "stored"),
                     "--operation-id", str(uuid.uuid4()), "--requested-by", str(self.operator.id),
                     "--reason", "Préparation console privée", "--gate-version", "1", "--show-code"]
        environment = {**os.environ, "DJANGO_SETTINGS_MODULE": "intentionally.invalid.settings"}
        completed = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", helper, str(script), *arguments],
                                  text=True, capture_output=True, env=environment, timeout=45)
        self.assertEqual(completed.returncode, 0, "Provision probe failed")
        self.assertEqual(json.loads(completed.stdout), {"first": 0, "second": 1, "state": "available",
            "strong_code": True, "secret_omitted_from_json": True, "plaintext_absent": True,
            "vault_unchanged": True, "second_stdout_empty": True})
