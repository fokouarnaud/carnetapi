"""Hosting ticks share the external restore barrier through SQL, SMTP and closure."""
from contextlib import closing
from datetime import datetime, timezone as utc
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase
from backups.tests import test_scheduler_worker_gate as fixtures
from backups.models import HostingConfiguration, HostingReminder
from backups.services.restore_gate import RestoreGateBusy


WORKER=r'''
import io,json,os,time
from pathlib import Path
from datetime import datetime,timezone
from unittest.mock import patch
import django
django.setup()
from django.conf import settings
from django.db import connection
from django.core.management import call_command,CommandError
from backups.services.hosting import hosting_tick,HostingUnavailable
from backups.services.restore_gate import hold_control,RestoreGateBusy,RestoreGateError
def protection():
 try:
  with hold_control(settings.RESTORE_GATE_PATH,settings.DATABASES['default']['NAME'],required_mode='open',expected_version=1):return 'unprotected'
 except RestoreGateBusy:return 'protected'
 except RestoreGateError:return 'unprotected'
sql_checks=[];close_checks=[];smtp_checks=[]
real_close=connection.close
def close():
 real_close();close_checks.append(protection())
connection.close=close
def sql(execute,statement,params,many,context):
 sql_checks.append(protection());return execute(statement,params,many,context)
def send(*args,**kwargs):
 smtp_checks.append(protection())
 if os.environ.get('HOST_SMTP_READY'):
  Path(os.environ['HOST_SMTP_READY']).touch()
  deadline=time.monotonic()+25
  while not Path(os.environ['HOST_SMTP_RELEASE']).exists():
   if time.monotonic()>deadline:raise TimeoutError('private test watchdog')
   time.sleep(.02)
 return 1
try:
 with connection.execute_wrapper(sql),patch('django.utils.timezone.now',return_value=datetime(2026,11,3,tzinfo=timezone.utc)),patch('django.core.mail.message.EmailMessage.send',side_effect=send):
  if os.environ.get('HOST_COMMAND'):
   out=io.StringIO();call_command('hosting_reminder_step',stdout=out);result=json.loads(out.getvalue())
  else:result={'status':hosting_tick()}
except HostingUnavailable:result={'error':'hosting_unavailable'}
except CommandError as error:result={'command_error':str(error)}
result.update(sql_checks=sql_checks,close_checks=close_checks,smtp_checks=smtp_checks,connection_closed=connection.connection is None)
print(json.dumps(result))
'''


class HostingWorkerTests(TransactionTestCase):
    initialize=fixtures.SchedulerWorkerGateTests.initialize
    enter=fixtures.SchedulerWorkerGateTests.enter
    environment=fixtures.SchedulerWorkerGateTests.environment

    def setUp(self):
        fixtures.SchedulerWorkerGateTests.setUp(self)
        with patch("django.utils.timezone.now",return_value=datetime(2026,11,1,tzinfo=utc.utc)):
            current=HostingConfiguration.objects.create(expires_at=datetime(2026,11,10,15,tzinfo=utc.utc),
                recipient="operator@example.test",language="fr",evidence_reference="Fixture privée",updated_by=self.actor)
            for days in (7,2):HostingReminder.objects.create(expires_at=current.expires_at,lead_days=days)
        with closing(sqlite3.connect(self.business)) as db:connection.connection.backup(db)
        self.settings_file.write_text(self.settings_file.read_text(encoding="utf-8")+
            "\nHOSTING_REMINDER_ACTOR_ID="+repr(str(self.actor.id))+"\nDEFAULT_FROM_EMAIL='sender@example.test'\n",encoding="utf-8")

    def worker(self,command=False):
        completed=subprocess.run([sys.executable,"-X","utf8","-c",WORKER],capture_output=True,text=True,
            env=self.environment(HOST_COMMAND="1" if command else ""),timeout=45)
        self.assertEqual(completed.returncode,0,completed.stderr)
        return json.loads(completed.stdout)

    def test_missing_or_closed_gate_refuses_before_sql_and_preserves_business_bytes(self):
        before=hashlib.sha256(self.business.read_bytes()).hexdigest()
        result=self.worker()
        self.assertEqual((result["error"],result["sql_checks"],result["smtp_checks"]),("hosting_unavailable",[],[]))
        self.assertFalse(self.gate.exists())
        self.initialize();self.enter()
        result=self.worker(command=True)
        self.assertEqual((result["command_error"],result["sql_checks"],result["smtp_checks"]),("hosting_reminder_unavailable",[],[]))
        self.assertEqual(hashlib.sha256(self.business.read_bytes()).hexdigest(),before)

    def test_console_sql_smtp_and_connection_closure_all_hold_the_normal_lease(self):
        self.initialize()
        result=self.worker(command=True)
        self.assertEqual(result["status"],"accepted")
        self.assertGreater(len(result["sql_checks"]),0)
        self.assertEqual(set(result["sql_checks"]),{"protected"})
        self.assertEqual(result["smtp_checks"],["protected"])
        self.assertEqual(result["close_checks"],["protected"])
        self.assertTrue(result["connection_closed"])
        self.assertEqual(self.enter()["mode"],"maintenance")

    def test_smtp_wait_blocks_maintenance_and_competing_tick_cannot_send_again(self):
        self.initialize()
        ready,release=self.root/"smtp-ready",self.root/"smtp-release"
        process=subprocess.Popen([sys.executable,"-X","utf8","-c",WORKER],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True,env=self.environment(HOST_SMTP_READY=str(ready),HOST_SMTP_RELEASE=str(release)))
        try:
            deadline=time.monotonic()+20
            while not ready.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(ready.exists(),"hosting SMTP fixture did not start")
            with self.assertRaises(RestoreGateBusy):self.enter()
            competing=self.worker()
            self.assertEqual(competing["status"],"busy")
            self.assertEqual(competing["smtp_checks"],[])
            with self.assertRaises(RestoreGateBusy):self.enter()
            release.touch()
            stdout,stderr=process.communicate(timeout=30)
            self.assertEqual(process.returncode,0,stderr)
            result=json.loads(stdout)
            self.assertEqual((result["status"],result["smtp_checks"]),("accepted",["protected"]))
            with closing(sqlite3.connect(self.business)) as db:
                self.assertEqual(db.execute("SELECT state,COUNT(*) FROM backups_hostingreminderattempt GROUP BY state").fetchall(),[("accepted",1)])
            self.assertEqual(self.enter()["mode"],"maintenance")
        finally:
            release.touch()
            if process.poll() is None:process.kill();process.communicate(timeout=10)
