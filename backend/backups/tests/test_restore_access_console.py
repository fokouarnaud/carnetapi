"""The hosting console reviews one scope while the external gate stays closed."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import uuid
from django.contrib.auth.hashers import PBKDF2PasswordHasher
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from backups.services.restore_gate import initialize_gate, enter_maintenance, read_gate, abort_maintenance, hold_control
from backups.services.restore_quarantine import _suspend
from identity.models import Shop, User


class RestoreAccessConsoleTests(TransactionTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="restore-access-console-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database, self.gate = self.root/"candidate.sqlite3", self.root/"gate.sqlite3"
        self.owner = User.objects.create_user(username="archived-owner")
        self.old_technical = User.objects.create_superuser(username="archived-technical", password=None)
        self.shops = [Shop.objects.create(owner=self.owner, name=name) for name in ("A", "B")]
        with closing(sqlite3.connect(self.database)) as db:
            connection.connection.backup(db)
        self.generation_id = uuid.uuid4()
        _suspend(self.database, timezone.now().replace(tzinfo=None).isoformat(sep=" "),
            generation_id=self.generation_id, operation_id=uuid.uuid4(), backup_id=uuid.uuid4(), requested_by=self.old_technical.id)
        self.password = "console-fixture-private-92!"
        self.technical = User.objects.create_superuser(username="current-technical", password=None)
        self.technical.password = PBKDF2PasswordHasher().encode(self.password, uuid.uuid4().hex)
        self.technical.save()
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM core_user WHERE id=%s", [self.technical.id.hex])
            columns = [item[0] for item in cursor.description]
            row = cursor.fetchone()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO core_user ("+",".join(columns)+") VALUES ("+",".join("?" for _ in columns)+")", row)
            self.reviews = {shop:review for shop,review in db.execute(
                "SELECT shop_id,id FROM backups_restoreaccessreview WHERE generation_id=?", [self.generation_id.hex])}
        initialize_gate(self.gate, self.database, operation_id=uuid.uuid4(), requested_by=self.old_technical.id, reason="Fixture")
        enter_maintenance(self.gate, operation_id=uuid.uuid4(), requested_by=self.old_technical.id, reason="Incident", expected_version=1)
        self.operation_id = uuid.uuid4()
        self.script = Path(__file__).resolve().parents[3]/"tools/review_restore_access.py"

    def command(self, action="inspect", *, password=None, username=None, database=None, gate_version=2, extra=()):
        completed = subprocess.run([sys.executable, "-I", "-X", "utf8", str(self.script), action,
            "--gate", str(self.gate), "--database", str(database or self.database), "--gate-version", str(gate_version),
            "--username", username or self.technical.username, "--stdin-json", *map(str,extra)],
            input=json.dumps({"password":self.password if password is None else password}), text=True, capture_output=True,
            env={**os.environ, "DJANGO_SETTINGS_MODULE":"intentionally.invalid", "APP_DB_PATH":str(self.root/"trap.sqlite3"),
                 "APP_EMAIL_CONFIG_FILE":str(self.root/"missing-email-config.json")}, timeout=60)
        self.assertNotIn(self.password, completed.stdout+completed.stderr)
        self.assertFalse((self.root/"trap.sqlite3").exists())
        return completed

    def decision_args(self, index=0, **values):
        data = {"review-id":str(uuid.UUID(self.reviews[self.shops[index].id.hex])), "generation-id":str(self.generation_id),
            "operation-id":str(self.operation_id), "previous-version":0, "state":"confirmed", "reason":"Contrôle actuel motivé", **values}
        return [item for key,value in data.items() for item in ("--"+key, str(value))]

    def test_paginated_inspection_is_read_only_and_does_not_open_the_gate(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        first = self.command(extra=("--page-size",1))
        self.assertEqual(first.returncode, 0, first.stderr)
        page = json.loads(first.stdout)
        self.assertEqual(page["generation_id"], str(self.generation_id))
        self.assertEqual((page["count"],page["page"],page["page_size"]), (2,1,1))
        self.assertEqual(page["results"][0]["state"], "pending")
        second = self.command(extra=("--page-size",1,"--page",2))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotEqual(page["results"][0]["id"], json.loads(second.stdout)["results"][0]["id"])
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)
        self.assertEqual(read_gate(self.gate)["mode"], "maintenance")
        self.assertFalse(page["activation_allowed"])

    def test_confirmation_is_password_authenticated_scoped_and_idempotent_without_reopening(self):
        results = [self.command("decide", extra=self.decision_args()) for _ in range(2)]
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(results[0].stdout), json.loads(results[1].stdout))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT state,reason,evidence_ref,version FROM backups_restoreaccessdecision").fetchall(),
                [("confirmed","Contrôle actuel motivé","",1)])
            self.assertEqual(db.execute("SELECT is_active,is_staff,is_superuser FROM core_user WHERE id=?",[self.owner.id.hex]).fetchone(), (1,0,0))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_restoreaccessdecision d JOIN backups_restoreaccessreview r ON r.id=d.review_id WHERE r.shop_id=?",[self.shops[1].id.hex]).fetchone()[0], 0)
        changed = self.command("decide", extra=self.decision_args(reason="Autre contrôle"))
        self.assertNotEqual(changed.returncode, 0)
        self.assertEqual(read_gate(self.gate)["mode"], "maintenance")

    def test_bad_credentials_gate_generation_and_schema_fail_before_any_decision(self):
        for options in ({"password":"wrong"}, {"username":self.old_technical.username},
                        {"username":self.owner.username}, {"gate_version":1},
                        {"database":self.root/"missing.sqlite3"}):
            with self.subTest(options=options):
                result = self.command("decide", extra=self.decision_args(), **options)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("error", json.loads(result.stderr))
        wrong_generation = self.command("decide", extra=self.decision_args(**{"generation-id":uuid.uuid4()}))
        self.assertNotEqual(wrong_generation.returncode, 0)
        self.assertFalse((self.root/"missing.sqlite3").exists())
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TRIGGER surprise AFTER UPDATE ON core_user BEGIN DELETE FROM backups_restoreaccessdecision; END")
        invalid = self.command("decide", extra=self.decision_args())
        self.assertNotEqual(invalid.returncode, 0)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_restoreaccessdecision").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT is_active FROM core_user WHERE id=?",[self.owner.id.hex]).fetchone()[0], 0)

    def test_current_role_is_required_even_with_the_correct_password_and_an_active_account(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE core_user SET is_active=1,password=? WHERE id=?", [self.technical.password,self.owner.id.hex])
        denied = self.command(username=self.owner.username)
        self.assertNotEqual(denied.returncode, 0)
        for field in ("is_active", "is_staff", "is_superuser"):
            with self.subTest(field=field):
                with closing(sqlite3.connect(self.database)) as db, db:
                    db.execute("UPDATE core_user SET is_active=1,is_staff=1,is_superuser=1 WHERE id=?",[self.technical.id.hex])
                    db.execute(f"UPDATE core_user SET {field}=0 WHERE id=?",[self.technical.id.hex])
                denied = self.command("decide", extra=self.decision_args())
                self.assertNotEqual(denied.returncode, 0)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_restoreaccessdecision").fetchone()[0], 0)

    def test_open_gate_and_another_console_controller_exclude_the_command(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with hold_control(self.gate, self.database, required_mode="maintenance", expected_version=2):
            denied = self.command("decide", extra=self.decision_args())
            self.assertNotEqual(denied.returncode, 0)
            self.assertEqual(json.loads(denied.stderr)["error"], "restore_access_gate_unavailable")
        abort_maintenance(self.gate, operation_id=uuid.uuid4(), requested_by=self.old_technical.id,
            reason="Fixture inchangée", expected_version=2)
        denied = self.command("decide", gate_version=3, extra=self.decision_args())
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)

    def test_preconfigured_django_cannot_open_another_database_under_the_selected_gate(self):
        trap = self.root/"preconfigured-trap.sqlite3"
        helper = """
import os,sys,runpy
sys.path.insert(0,sys.argv[1])
os.environ['DJANGO_SETTINGS_MODULE']='config.test_settings'
os.environ['APP_DB_PATH']=sys.argv[2]
import django
django.setup()
main=runpy.run_path(sys.argv[3],run_name='console_binding_probe')['main']
raise SystemExit(main(sys.argv[4:]))
"""
        result = subprocess.run([sys.executable,"-I","-X","utf8","-c",helper,
            str(self.script.parent.parent/"backend"),str(trap),str(self.script),"decide",
            "--gate",str(self.gate),"--database",str(self.database),"--gate-version","2",
            "--username",self.technical.username,"--stdin-json",*self.decision_args()],
            input=json.dumps({"password":self.password}),text=True,capture_output=True,timeout=60)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(trap.exists(), "The console must refuse a preloaded ORM binding before opening it")
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_restoreaccessdecision").fetchone()[0],0)

    def test_inspection_filters_current_states_and_rejects_unbounded_or_out_of_range_pages(self):
        approved = self.command("decide", extra=self.decision_args())
        self.assertEqual(approved.returncode, 0, approved.stderr)
        filtered = self.command(extra=("--state","pending","--shop-id",self.shops[1].id))
        self.assertEqual(filtered.returncode, 0, filtered.stderr)
        payload = json.loads(filtered.stdout)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["shop_id"], str(self.shops[1].id))
        for options in (("--page-size",101), ("--page-size",0), ("--page",0), ("--page",3,"--page-size",1)):
            with self.subTest(options=options):
                invalid = self.command(extra=options)
                self.assertNotEqual(invalid.returncode, 0)
        refusal = self.command("decide",extra=self.decision_args(**{"previous-version":1,"state":"refused","operation-id":uuid.uuid4()}))
        self.assertEqual(refusal.returncode, 0, refusal.stderr)
        latest = self.command(extra=("--state","refused"))
        self.assertEqual(latest.returncode, 0, latest.stderr)
        self.assertEqual(json.loads(latest.stdout)["results"][0]["version"], 2)
