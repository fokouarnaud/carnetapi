"""Independent SQLite workers must acknowledge the same captures once."""
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

from backups.tests import test_recovery_journal_api as fixtures
from identity.models import DeviceSession


class RecoveryJournalConcurrencyTests(TransactionTestCase):
    setUp=fixtures.RecoveryJournalApiTests.setUp
    authenticate=fixtures.RecoveryJournalApiTests.authenticate
    generation=fixtures.RecoveryJournalApiTests.generation
    record=fixtures.RecoveryJournalApiTests.record
    device=fixtures.RecoveryJournalApiTests.device
    review=fixtures.RecoveryJournalApiTests.review
    payload=fixtures.RecoveryJournalApiTests.payload

    def test_two_processes_replay_copy_or_conflict_without_duplicate_or_partial_capture(self):
        backend=Path(__file__).resolve().parents[2]
        worker="""
import json,os,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
os.environ['DJANGO_SETTINGS_MODULE']='config.test_settings'
os.environ['APP_DB_PATH']=sys.argv[2]
import django
django.setup()
from identity.models import User
from backups.services.recovery_journal import capture_recovery_journal
from common.services.errors import OperationConflict
data=json.loads(sys.stdin.read())
Path(sys.argv[3]).touch()
deadline=time.monotonic()+30
while not Path(sys.argv[4]).exists():
 if time.monotonic()>deadline:raise RuntimeError('worker barrier timed out')
 time.sleep(.01)
try:
 result,created=capture_recovery_journal(shop_id=data['shop'],actor=User.objects.get(id=data['actor']),
  session_id=data['session'],data=data['request'])
 print(json.dumps({'created':created,'receipt':result}))
except OperationConflict:print(json.dumps({'conflict':True}))
"""
        session=DeviceSession.objects.get(installation__user=self.owner)
        with TemporaryDirectory(prefix="recovery-journal-race-") as directory:
            root=Path(directory)
            for mode in ("same-command","another-copy","changed-source"):
                with self.subTest(mode=mode):
                    database=root/(mode+".sqlite3")
                    with closing(sqlite3.connect(database)) as target:connection.connection.backup(target)
                    start=root/(mode+"-start")
                    ready=[root/f"{mode}-ready-{index}" for index in range(2)]
                    body=self.payload()
                    def run(index):
                        request=json.loads(json.dumps(body))
                        if index and mode!="same-command":request["operation_id"]=str(uuid.uuid4())
                        if index and mode=="changed-source":request["operations"][0]["payload"]["amount_fcfa"]=9999
                        data={"actor":str(self.owner.id),"shop":str(self.a.id),"session":str(session.id),"request":request}
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
                    self.assertEqual(sum(item.get("created",False) for item in results),2 if mode=="another-copy" else 1)
                    if mode=="same-command":self.assertEqual(results[0]["receipt"],results[1]["receipt"])
                    if mode=="another-copy":
                        self.assertEqual(results[0]["receipt"]["operations"],results[1]["receipt"]["operations"])
                        self.assertEqual(sorted(item["receipt"]["new_operation_count"] for item in results),[0,2])
                    if mode=="changed-source":self.assertEqual(sum(item.get("conflict",False) for item in results),1)
                    with closing(sqlite3.connect(database)) as db:
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_recoveryjournaloperation").fetchone()[0],2)
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_recoveryjournaldeposit").fetchone()[0],2 if mode=="another-copy" else 1)
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_sale").fetchone()[0],0)
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_cashmovement").fetchone()[0],0)
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_stockmovement").fetchone()[0],0)
