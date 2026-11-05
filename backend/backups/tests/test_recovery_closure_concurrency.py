"""Independent SQLite processes may record one closure, with one frozen acknowledgement."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import uuid

from django.db import connection
from django.test import TransactionTestCase

from backups.tests import test_recovery_closures as fixtures


class RecoveryClosureConcurrencyTests(TransactionTestCase):
    setUp=fixtures.RecoveryClosureTests.setUp
    preview=fixtures.RecoveryClosureTests.preview
    command=fixtures.RecoveryClosureTests.command

    def test_same_command_and_competing_closures_commit_once_across_two_processes(self):
        backend=Path(__file__).resolve().parents[2]
        worker="""
import json,os,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
os.environ['DJANGO_SETTINGS_MODULE']='config.test_settings'
os.environ['APP_DB_PATH']=sys.argv[2]
import django
django.setup()
from identity.models import User,DeviceSession
from backups.services.recovery_closures import close_recovery
from common.services.errors import OperationConflict
data=json.loads(sys.stdin.read())
Path(sys.argv[3]).touch()
deadline=time.monotonic()+30
while not Path(sys.argv[4]).exists():
 if time.monotonic()>deadline:raise RuntimeError('worker barrier timed out')
 time.sleep(.01)
try:
 receipt,created=close_recovery(actor=User.objects.get(id=data['actor']),
  session=DeviceSession.objects.get(id=data['session']),data=data['request'])
 print(json.dumps({'created':created,'receipt':receipt}))
except OperationConflict:print(json.dumps({'conflict':True}))
"""
        with TemporaryDirectory(prefix="recovery-closure-race-") as directory:
            root=Path(directory)
            for mode in ("same-command","competing-declarations"):
                with self.subTest(mode=mode):
                    database=root/(mode+".sqlite3")
                    with closing(sqlite3.connect(database)) as target:connection.connection.backup(target)
                    start=root/(mode+"-start")
                    ready=[root/f"{mode}-ready-{index}" for index in range(2)]
                    body=self.command()
                    def run(index):
                        request={**body}
                        if index and mode=="competing-declarations":
                            request["operation_id"]=str(uuid.uuid4())
                            request["missing_device_ids"]=[str(self.other.id)]
                        data={"actor":str(self.technical.id),"session":str(self.live.id),"request":request}
                        result=subprocess.run([sys.executable,"-I","-X","utf8","-c",worker,str(backend),str(database),
                            str(ready[index]),str(start)],input=json.dumps(data),text=True,capture_output=True,timeout=60)
                        self.assertEqual(result.returncode,0,result.stderr)
                        return json.loads(result.stdout)
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        futures=[pool.submit(run,index) for index in range(2)]
                        deadline=time.monotonic()+30
                        while not all(path.exists() for path in ready) and time.monotonic()<deadline:time.sleep(.01)
                        start.touch()
                        self.assertTrue(all(path.exists() for path in ready))
                        results=[future.result() for future in futures]
                    self.assertEqual(sum(item.get("created",False) for item in results),1)
                    if mode=="same-command":self.assertEqual(results[0]["receipt"],results[1]["receipt"])
                    else:self.assertEqual(sum(item.get("conflict",False) for item in results),1)
                    with closing(sqlite3.connect(database)) as db:
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_recoveryclosure").fetchone()[0],1)
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_activity").fetchone()[0],0)
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_cashmovement").fetchone()[0],0)
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_stockmovement").fetchone()[0],0)
