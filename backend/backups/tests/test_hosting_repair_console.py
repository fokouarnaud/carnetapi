"""A drained exclusive gate permits classification of an interrupted SMTP attempt."""
from contextlib import closing
from datetime import timedelta
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
from backups.models import HostingConfiguration, HostingReminder, HostingReminderAttempt
from backups.services.restore_gate import initialize_gate, acquire_request, read_gate, enter_maintenance
from identity.models import User


PROBED_CONSOLE=r'''
import importlib.util,json,sys
from pathlib import Path
script,mode,report=Path(sys.argv[1]),sys.argv[2],Path(sys.argv[3])
sys.path.insert(0,str(script.parent.parent/'backend'))
from backups.services.restore_gate import hold_control,RestoreGateBusy,RestoreGateError
gate,database=Path(sys.argv[sys.argv.index('--gate')+1]),Path(sys.argv[sys.argv.index('--database')+1])
checks={'sql':[],'close':[],'smtp':0}
def protected():
 try:
  with hold_control(gate,database,required_mode='open',expected_version=1):return False
 except RestoreGateBusy:return True
 except RestoreGateError:return False
import django
from django.db.backends.utils import CursorWrapper
from django.db.backends.base.base import BaseDatabaseWrapper
from django.core.mail.message import EmailMessage
real_execute,real_close=CursorWrapper.execute,BaseDatabaseWrapper.close
def execute(self,sql,params=None):
 checks['sql'].append(protected())
 if mode=='sql_failure' and sql.startswith('UPDATE "backups_hostingreminder"'):raise RuntimeError('private injected SQL failure')
 return real_execute(self,sql,params)
def close(self):
 real_close(self);checks['close'].append(protected())
def mail(*args,**kwargs):
 checks['smtp']+=1;raise AssertionError('Repair must not send SMTP')
CursorWrapper.execute,BaseDatabaseWrapper.close,EmailMessage.send=execute,close,mail
if mode=='startup_failure':
 def startup():raise RuntimeError('private startup failure')
 django.setup=startup
spec=importlib.util.spec_from_file_location('probed_hosting_repair',script)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
status=module.main(sys.argv[4:])
report.write_text(json.dumps(checks),encoding='utf-8')
raise SystemExit(status)
'''


class HostingRepairConsoleTests(TransactionTestCase):
    def setUp(self):
        self.directory=TemporaryDirectory(prefix="hosting-repair-");self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.database,self.gate=self.root/"business.sqlite3",self.root/"gate.sqlite3"
        self.password="private-hosting-repair-fixture-83!"
        self.actor=User.objects.create_superuser(username="repair-operator",password=None)
        self.actor.password=PBKDF2PasswordHasher().encode(self.password,uuid.uuid4().hex);self.actor.save()
        self.config=HostingConfiguration.objects.create(expires_at=timezone.now()+timedelta(days=7),recipient="operator@example.test",
            language="fr",evidence_reference="Console privée",updated_by=self.actor)
        self.reminder=HostingReminder.objects.create(expires_at=self.config.expires_at,lead_days=7)
        self.attempt=HostingReminderAttempt.objects.create(reminder=self.reminder,number=1,actor=self.actor,
            configuration_version=1,recipient="operator@example.test",sender="sender@example.test",language="fr",
            message_id="<private-interrupted@example.test>",subject="Rappel privé",body="Contenu d'origine à conserver",
            started_at=timezone.now()-timedelta(minutes=1),request={"origin":"private fixture"})
        self.reminder.current_attempt=self.attempt;self.reminder.state="sending";self.reminder.version=1;self.reminder.save()
        with closing(sqlite3.connect(self.database)) as db:connection.connection.backup(db)
        initialize_gate(self.gate,self.database,operation_id=uuid.uuid4(),requested_by=self.actor.id,reason="Fixture privée")
        self.operation=uuid.uuid4()
        self.script=Path(__file__).resolve().parents[3]/"tools/repair_hosting_reminder.py"

    def command(self,*,password=None,username=None,database=None,gate_version=1,operation=None,reason="Processus arrêté, résultat SMTP inconnu",probe=None):
        program=[sys.executable,"-I","-X","utf8",str(self.script)]
        if probe:program=[sys.executable,"-I","-X","utf8","-c",PROBED_CONSOLE,str(self.script),probe,str(self.root/"probe.json")]
        result=subprocess.run([*program,"--gate",str(self.gate),
            "--database",str(database or self.database),"--gate-version",str(gate_version),"--username",username or self.actor.username,
            "--attempt-id",str(self.attempt.id),"--operation-id",str(operation or self.operation),"--reason",reason,
            "--acknowledge-uncertain","--stdin-json"],input=json.dumps({"password":self.password if password is None else password}),
            text=True,capture_output=True,timeout=60,env={**os.environ,"DJANGO_SETTINGS_MODULE":"invalid.fixture.settings",
                "APP_DB_PATH":str(self.root/"trap.sqlite3"),"APP_EMAIL_CONFIG_FILE":str(self.root/"missing-mail.json")})
        self.assertNotIn(self.password,result.stdout+result.stderr)
        self.assertFalse((self.root/"trap.sqlite3").exists())
        return result

    def rows(self,query,params=()):
        with closing(sqlite3.connect(self.database)) as db:return db.execute(query,params).fetchall()

    def test_repair_preserves_message_origin_and_gate_then_replays_without_a_second_decision(self):
        before=self.rows("SELECT actor_id,recipient,sender,message_id,subject,body,request,started_at FROM backups_hostingreminderattempt")
        first=self.command();self.assertEqual(first.returncode,0,first.stderr)
        receipt=json.loads(first.stdout)
        self.assertEqual((receipt["before_state"],receipt["after_state"]),("sending","uncertain"))
        self.assertFalse(receipt["smtp_sent"])
        self.assertEqual(receipt["requested_by"],str(self.actor.id))
        self.assertEqual((receipt["gate_mode"],receipt["gate_version"]),("open",1))
        self.assertEqual(self.rows("SELECT actor_id,recipient,sender,message_id,subject,body,request,started_at FROM backups_hostingreminderattempt"),before)
        self.assertEqual(self.rows("SELECT state,failure_code,finished_at IS NOT NULL FROM backups_hostingreminderattempt"),[("uncertain","smtp_worker_interrupted",1)])
        self.assertEqual(self.rows("SELECT state,version,current_attempt_id FROM backups_hostingreminder"),[("uncertain",1,self.attempt.id.hex)])
        same=self.command();self.assertEqual(same.returncode,0,same.stderr)
        self.assertEqual(json.loads(same.stdout),receipt)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM backups_hostingreminderrepair"),[(1,)])
        self.assertNotEqual(self.command(reason="Autre décision").returncode,0)
        self.assertEqual(read_gate(self.gate)["version"],1)
        with acquire_request(self.gate,self.database):pass

    def test_live_worker_lease_prevents_repair_without_touching_business_bytes(self):
        before=hashlib.sha256(self.database.read_bytes()).hexdigest()
        with acquire_request(self.gate,self.database):
            result=self.command()
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(json.loads(result.stderr),{"error":"hosting_repair_gate_unavailable"})
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(),before)
        self.assertEqual(self.rows("SELECT state FROM backups_hostingreminderattempt"),[("sending",)])

    def test_credentials_wrong_database_version_and_maintenance_refuse_before_repair(self):
        before=hashlib.sha256(self.database.read_bytes()).hexdigest()
        for options in ({"password":"incorrect"},{"username":"missing"},{"gate_version":2},{"database":self.root/"absent.sqlite3"}):
            with self.subTest(options=tuple(options)):
                result=self.command(**options);self.assertNotEqual(result.returncode,0)
                self.assertIn("error",json.loads(result.stderr))
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(),before)
        enter_maintenance(self.gate,operation_id=uuid.uuid4(),requested_by=self.actor.id,reason="Maintenance privée",expected_version=1)
        self.assertNotEqual(self.command(gate_version=2).returncode,0)
        self.assertEqual(read_gate(self.gate)["mode"],"maintenance")
        self.assertEqual(self.rows("SELECT state FROM backups_hostingreminderattempt"),[("sending",)])

    def test_current_role_is_required_even_when_replaying_an_existing_repair(self):
        for field in ("is_active","is_staff","is_superuser"):
            with self.subTest(field=field):
                with closing(sqlite3.connect(self.database)) as db,db:db.execute(f"UPDATE core_user SET {field}=0 WHERE id=?",[self.actor.id.hex])
                self.assertNotEqual(self.command().returncode,0)
                with closing(sqlite3.connect(self.database)) as db,db:db.execute(f"UPDATE core_user SET {field}=1 WHERE id=?",[self.actor.id.hex])
        result=self.command();self.assertEqual(result.returncode,0,result.stderr)
        with closing(sqlite3.connect(self.database)) as db,db:db.execute("UPDATE core_user SET is_superuser=0 WHERE id=?",[self.actor.id.hex])
        self.assertNotEqual(self.command().returncode,0)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM backups_hostingreminderrepair"),[(1,)])

    def test_finished_attempt_or_untrusted_schema_cannot_be_reclassified(self):
        with closing(sqlite3.connect(self.database)) as db,db:
            db.execute("UPDATE backups_hostingreminderattempt SET state='accepted',finished_at=started_at")
            db.execute("UPDATE backups_hostingreminder SET state='accepted'")
        self.assertNotEqual(self.command().returncode,0)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM backups_hostingreminderrepair"),[(0,)])
        with closing(sqlite3.connect(self.database)) as db,db:
            db.execute("CREATE TRIGGER untrusted_hosting AFTER UPDATE ON backups_hostingreminderattempt BEGIN DELETE FROM backups_hostingreminderrepair; END")
        result=self.command();self.assertNotEqual(result.returncode,0)
        self.assertEqual(json.loads(result.stderr),{"error":"hosting_repair_schema_invalid"})
        self.assertEqual(self.rows("SELECT state FROM backups_hostingreminderattempt"),[("accepted",)])

    def test_sql_and_connection_closure_remain_protected_and_repair_cannot_send_mail(self):
        result=self.command(probe="success")
        self.assertEqual(result.returncode,0,result.stderr)
        checks=json.loads((self.root/"probe.json").read_text())
        self.assertGreater(len(checks["sql"]),0)
        self.assertTrue(all(checks["sql"]))
        self.assertEqual(checks["close"],[True])
        self.assertEqual(checks["smtp"],0)
        with acquire_request(self.gate,self.database):pass

    def test_sql_failure_rolls_back_and_closes_before_releasing_the_exclusive_gate(self):
        result=self.command(probe="sql_failure")
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(json.loads(result.stderr),{"error":"hosting_repair_failed"})
        checks=json.loads((self.root/"probe.json").read_text())
        self.assertTrue(all(checks["sql"]))
        self.assertEqual(checks["close"],[True])
        self.assertEqual(checks["smtp"],0)
        self.assertEqual(self.rows("SELECT state,finished_at,failure_code FROM backups_hostingreminderattempt"),[("sending",None,"")])
        self.assertEqual(self.rows("SELECT state FROM backups_hostingreminder"),[("sending",)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM backups_hostingreminderrepair"),[(0,)])
        with acquire_request(self.gate,self.database):pass

    def test_startup_failure_is_sanitized_before_sql_and_releases_the_gate(self):
        before=hashlib.sha256(self.database.read_bytes()).hexdigest()
        result=self.command(probe="startup_failure")
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(json.loads(result.stderr),{"error":"hosting_repair_failed"})
        checks=json.loads((self.root/"probe.json").read_text())
        self.assertEqual((checks["sql"],checks["smtp"]),([],0))
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(),before)
        with acquire_request(self.gate,self.database):pass
