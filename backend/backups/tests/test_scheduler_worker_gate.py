"""A scheduler invoked outside HTTP must drain before a restore can start."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import uuid

from cryptography.fernet import Fernet
from django.db import connection
from django.test import TransactionTestCase

from backups.models import BackupConfiguration
from backups.services.restore_gate import initialize_gate, enter_maintenance, read_gate, RestoreGateBusy
from identity.models import User


WORKER = r'''
import io, json, os, sys, time, uuid
import django
django.setup()
from django.conf import settings
from django.db import connection
from backups.services.restore_gate import hold_control, RestoreGateBusy, RestoreGateError
from backups.services.scheduler import scheduler_tick
from backups.services.scheduler_errors import SchedulerUnavailable
from django.core.management import call_command, CommandError

submissions=[]
if os.environ.get("WORKER_SMTP_READY"):
    from pathlib import Path
    import backups.services.sending
    def submit(message):
        submissions.append(True)
        Path(os.environ["WORKER_SMTP_READY"]).write_text("ready")
        deadline=time.monotonic()+25
        while not Path(os.environ["WORKER_SMTP_RELEASE"]).exists():
            if time.monotonic()>deadline:
                raise TimeoutError("fixture SMTP watchdog")
            time.sleep(.02)
    backups.services.sending.submit_smtp=submit

checks=[]
close_checks=[]
def protection():
    try:
        with hold_control(settings.RESTORE_GATE_PATH,settings.DATABASES["default"]["NAME"],
                required_mode="open",expected_version=1):
            return "unprotected"
    except RestoreGateBusy:
        return "protected"
    except RestoreGateError:
        return "unprotected"

real_close=connection.close
def observed_close():
    real_close()
    close_checks.append(protection())
connection.close=observed_close

def before_sql(execute, sql, params, many, context):
    checks.append(protection())
    return execute(sql,params,many,context)

try:
    with connection.execute_wrapper(before_sql):
        if os.environ.get("WORKER_COMMAND"):
            output=io.StringIO()
            call_command("backup_scheduler_step",stdout=output)
            result=json.loads(output.getvalue())
        else:
            result={"status":scheduler_tick()}
except RestoreGateError as error:
    result={"error":str(error)}
except SchedulerUnavailable:
    result={"error":"scheduler_unavailable"}
except CommandError as error:
    result={"command_error":str(error)}
result.update(sql_checks=checks,close_checks=close_checks,connection_closed=connection.connection is None,
    smtp_submissions=len(submissions))
print(json.dumps(result))
'''


class SchedulerWorkerGateTests(TransactionTestCase):
    def setUp(self):
        self.directory=TemporaryDirectory(prefix="scheduler-worker-gate-")
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.business=self.root/"business.sqlite3"
        self.gate=self.root/"gate.sqlite3"
        self.actor=User.objects.create_superuser(username="worker-technical",password=None)
        BackupConfiguration.objects.create(singleton=1,enabled=False)
        with closing(sqlite3.connect(self.business)) as db:
            connection.connection.backup(db)
        self.backend=Path(__file__).resolve().parents[2]
        self.settings_file=self.root/"worker_fixture_settings.py"
        self.settings_file.write_text(
            "from config.test_settings import *\n"
            "DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':"+repr(str(self.business))+
            ",'OPTIONS':{'timeout':1,'transaction_mode':'IMMEDIATE'}}}\n"
            "RESTORE_GATE_PATH="+repr(str(self.gate))+"\n"
            "BACKUP_SCHEDULER_ACTOR_ID="+repr(str(self.actor.id))+"\n"
            "BACKUP_ROOT="+repr(str(self.root/"archives"))+"\n"
            "BACKUP_ENCRYPTION_KEY="+repr(Fernet.generate_key())+"\n"
            "EMAIL_HOST_USER='sender@example.test'\n",encoding="utf-8")

    def initialize(self):
        initialize_gate(self.gate,self.business,operation_id=uuid.uuid4(),requested_by=self.actor.id,reason="Fixture")

    def enter(self):
        return enter_maintenance(self.gate,operation_id=uuid.uuid4(),requested_by=self.actor.id,
            reason="Restore fixture",expected_version=1)

    def environment(self,**extra):
        return {**os.environ,"PYTHONPATH":os.pathsep.join((str(self.root),str(self.backend))),
            "DJANGO_SETTINGS_MODULE":"worker_fixture_settings",**extra}

    def run_worker(self,command=False):
        completed=subprocess.run([sys.executable,"-X","utf8","-c",WORKER],capture_output=True,text=True,
            env=self.environment(WORKER_COMMAND="1" if command else ""),timeout=45)
        self.assertEqual(completed.returncode,0,completed.stderr)
        return json.loads(completed.stdout)

    def test_maintenance_refuses_job_before_any_sql_or_scheduler_effect(self):
        self.initialize()
        self.enter()
        before=hashlib.sha256(self.business.read_bytes()).hexdigest()
        result=self.run_worker()
        self.assertEqual(result["error"],"scheduler_unavailable")
        self.assertEqual(result["sql_checks"],[])
        self.assertTrue(result["connection_closed"])
        self.assertEqual(hashlib.sha256(self.business.read_bytes()).hexdigest(),before)
        self.assertEqual(read_gate(self.gate)["mode"],"maintenance")

    def test_missing_gate_cannot_create_control_file_or_touch_business(self):
        before=hashlib.sha256(self.business.read_bytes()).hexdigest()
        result=self.run_worker()
        self.assertEqual(result["error"],"scheduler_unavailable")
        self.assertEqual(result["sql_checks"],[])
        self.assertFalse(self.gate.exists())
        self.assertEqual(hashlib.sha256(self.business.read_bytes()).hexdigest(),before)

    def test_gate_for_another_database_is_rejected_before_any_query(self):
        other=self.root/"another.sqlite3"
        other.write_bytes(self.business.read_bytes())
        initialize_gate(self.gate,other,operation_id=uuid.uuid4(),requested_by=self.actor.id,reason="Other database")
        before=hashlib.sha256(self.business.read_bytes()).hexdigest()
        result=self.run_worker()
        self.assertEqual(result["error"],"scheduler_unavailable")
        self.assertEqual(result["sql_checks"],[])
        self.assertEqual(hashlib.sha256(self.business.read_bytes()).hexdigest(),before)
        self.assertEqual(read_gate(self.gate)["mode"],"open")

    def test_real_job_holds_lease_for_all_sql_and_closes_connection_before_release(self):
        self.initialize()
        result=self.run_worker()
        self.assertEqual(result["status"],"disabled")
        self.assertGreater(len(result["sql_checks"]),0)
        self.assertEqual(set(result["sql_checks"]),{"protected"})
        self.assertTrue(result["connection_closed"])
        self.assertEqual(result["close_checks"],["protected"])
        self.assertEqual(self.enter()["mode"],"maintenance")
        with closing(sqlite3.connect(self.business)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_backupschedulerstate").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT lease_id,leased_until FROM backups_backupschedulerstate").fetchone(),(None,None))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_backupjob").fetchone()[0],0)

    def test_console_command_exposes_one_protected_step_with_small_json_result(self):
        self.initialize()
        result=self.run_worker(command=True)
        self.assertEqual(result["status"],"disabled")
        self.assertEqual(set(result["sql_checks"]),{"protected"})
        self.assertTrue(result["connection_closed"])
        self.assertEqual(self.enter()["mode"],"maintenance")

    def test_console_command_refuses_maintenance_with_stable_error_and_no_sql(self):
        self.initialize()
        self.enter()
        before=hashlib.sha256(self.business.read_bytes()).hexdigest()
        result=self.run_worker(command=True)
        self.assertEqual(result["command_error"],"backup_scheduler_unavailable")
        self.assertEqual(result["sql_checks"],[])
        self.assertEqual(hashlib.sha256(self.business.read_bytes()).hexdigest(),before)

    def test_manage_process_returns_json_or_a_nonzero_maintenance_error(self):
        self.initialize()
        command=[sys.executable,"-X","utf8",str(self.backend/"manage.py"),"backup_scheduler_step"]
        accepted=subprocess.run(command,capture_output=True,text=True,env=self.environment(),timeout=45)
        self.assertEqual(accepted.returncode,0,accepted.stderr)
        self.assertEqual(json.loads(accepted.stdout),{"status":"disabled"})
        self.assertEqual(self.enter()["mode"],"maintenance")
        before=hashlib.sha256(self.business.read_bytes()).hexdigest()
        denied=subprocess.run(command,capture_output=True,text=True,env=self.environment(),timeout=45)
        self.assertNotEqual(denied.returncode,0)
        self.assertEqual(denied.stdout,"")
        self.assertIn("backup_scheduler_unavailable",denied.stderr)
        self.assertNotIn("Traceback",denied.stderr)
        self.assertEqual(hashlib.sha256(self.business.read_bytes()).hexdigest(),before)

    def test_smtp_wait_blocks_maintenance_and_other_tick_cannot_send_again(self):
        self.initialize()
        with closing(sqlite3.connect(self.business)) as db,db:
            db.execute("UPDATE backups_backupconfiguration SET enabled=1,local_hour=0,local_minute=0")
        prepared=self.run_worker()
        self.assertEqual(prepared["status"],"prepared")
        ready,release=self.root/"smtp-ready",self.root/"smtp-release"
        process=subprocess.Popen([sys.executable,"-X","utf8","-c",WORKER],stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,text=True,env=self.environment(WORKER_SMTP_READY=str(ready),WORKER_SMTP_RELEASE=str(release)))
        try:
            deadline=time.monotonic()+20
            while not ready.exists() and process.poll() is None and time.monotonic()<deadline:
                time.sleep(.02)
            if not ready.exists():
                self.fail("SMTP fixture did not start: "+str(process.poll()))
            with self.assertRaises(RestoreGateBusy):
                self.enter()
            other=self.run_worker()
            self.assertEqual(other["status"],"busy")
            self.assertEqual(other["smtp_submissions"],0)
            with self.assertRaises(RestoreGateBusy):
                self.enter()
            release.write_text("continue")
            stdout,stderr=process.communicate(timeout=30)
            self.assertEqual(process.returncode,0,stderr)
            result=json.loads(stdout)
            self.assertEqual(result["status"],"accepted")
            self.assertEqual(result["smtp_submissions"],1)
            self.assertEqual(set(result["sql_checks"]),{"protected"})
            self.assertEqual(result["close_checks"],["protected"])
            self.assertTrue(result["connection_closed"])
        finally:
            release.touch(exist_ok=True)
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)
        self.assertEqual(self.enter()["mode"],"maintenance")
        with closing(sqlite3.connect(self.business)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_backupschedulerstate").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT lease_id,leased_until FROM backups_backupschedulerstate").fetchone(),(None,None))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_backupjob").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT cleanup_state FROM backups_backupjob").fetchone()[0],"cleaned")
            self.assertEqual(db.execute("SELECT state FROM backups_sendattempt").fetchall(),[("accepted",)])

    def test_job_error_also_closes_database_and_releases_the_restore_lease(self):
        self.initialize()
        with closing(sqlite3.connect(self.business)) as db,db:
            db.execute("UPDATE core_user SET is_active=0 WHERE id=?",[self.actor.id.hex])
        result=self.run_worker()
        self.assertEqual(result["error"],"scheduler_unavailable")
        self.assertEqual(set(result["sql_checks"]),{"protected"})
        self.assertTrue(result["connection_closed"])
        self.assertEqual(result["close_checks"],["protected"])
        self.assertEqual(self.enter()["mode"],"maintenance")
