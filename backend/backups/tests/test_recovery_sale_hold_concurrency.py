"""Real SQLite writers arbitrate review without duplicate departure or payment."""
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

from backups.tests import test_recovery_sale_hold_api as fixtures
from identity.models import DeviceSession


class RecoverySaleHoldConcurrencyTests(TransactionTestCase):
    setUp=fixtures.RecoverySaleHoldApiTests.setUp
    authenticate=fixtures.RecoverySaleHoldApiTests.authenticate
    generation=fixtures.RecoverySaleHoldApiTests.generation
    record=fixtures.RecoverySaleHoldApiTests.record
    device=fixtures.RecoverySaleHoldApiTests.device
    review=fixtures.RecoverySaleHoldApiTests.review
    payload=fixtures.RecoverySaleHoldApiTests.payload
    url=fixtures.RecoverySaleHoldApiTests.url
    source=fixtures.RecoverySaleHoldApiTests.source
    capture=fixtures.RecoverySaleHoldApiTests.capture
    preview=fixtures.RecoverySaleHoldApiTests.preview
    decide=fixtures.RecoverySaleHoldApiTests.decide
    stage=fixtures.RecoverySaleHoldApiTests.stage

    def test_competing_stage_approval_and_refusal_keep_one_reservation_and_one_terminal_decision(self):
        backend=Path(__file__).resolve().parents[2]
        capture=self.capture(self.source())
        session=DeviceSession.objects.get(installation__user=self.owner)
        worker="""
import json,os,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
os.environ['DJANGO_SETTINGS_MODULE']='config.test_settings'
os.environ['APP_DB_PATH']=sys.argv[2]
import django
django.setup()
from identity.models import User
from backups.services.recovery_sale_holds import decide_sale_hold
from backups.services.recovery_integration import integrate_recovery_operation
from common.services.errors import OperationConflict
data=json.loads(sys.stdin.read())
Path(sys.argv[3]).touch()
deadline=time.monotonic()+30
while not Path(sys.argv[4]).exists():
 if time.monotonic()>deadline:raise RuntimeError('worker barrier timed out')
 time.sleep(.01)
try:
 handler=decide_sale_hold if data['review'] else integrate_recovery_operation
 receipt,created=handler(shop_id=data['shop'],actor=User.objects.get(id=data['actor']),
  session_id=data['session'],capture_id=data['capture'],data=data['request'])
 print(json.dumps({'created':created,'receipt':receipt}))
except OperationConflict:print(json.dumps({'conflict':True}))
"""
        with TemporaryDirectory(prefix="recovery-hold-race-") as directory:
            root=Path(directory)
            for mode in ("stage-retry","approval-retry","approval-or-refusal"):
                with self.subTest(mode=mode):
                    if mode=="approval-retry":self.stage(capture)
                    preview=self.preview(capture)
                    database=root/(mode+".sqlite3")
                    with closing(sqlite3.connect(database)) as target:connection.connection.backup(target)
                    start=root/(mode+"-start")
                    ready=[root/f"{mode}-ready-{index}" for index in range(2)]
                    command={"operation_id":str(uuid.uuid4()),"previous_version":preview["version"],
                        "preview_token":preview["preview_token"],"reason":"Faits examinés en concurrence"}
                    def run(index):
                        request=dict(command)
                        review=mode=="stage-retry" or (mode=="approval-or-refusal" and index==1)
                        if review:
                            request.update(state="pending" if mode=="stage-retry" else "refused",previous_version=preview["review_version"])
                        if mode=="approval-or-refusal" and index:request["operation_id"]=str(uuid.uuid4())
                        data={"actor":str(self.owner.id),"shop":str(self.a.id),"session":str(session.id),
                            "capture":str(capture.id),"request":request,"review":review}
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
                    if mode!="approval-or-refusal":self.assertEqual(results[0]["receipt"],results[1]["receipt"])
                    else:self.assertEqual(sum(item.get("conflict",False) for item in results),1)
                    with closing(sqlite3.connect(database)) as db:
                        state=db.execute("SELECT state FROM backups_recoverysalehold").fetchone()[0]
                        self.assertIn(state,["pending"] if mode=="stage-retry" else ["validated"] if mode=="approval-retry" else ["validated","refused"])
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_stockreservation").fetchone()[0],1)
                        validated=state=="validated"
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_sale").fetchone()[0],int(validated))
                        self.assertEqual(db.execute("SELECT COALESCE(SUM(amount_fcfa),0) FROM core_cashmovement").fetchone()[0],500 if validated else 0)
                        balance=db.execute("SELECT SUM(quantity_milli) FROM core_stockmovement").fetchone()[0]
                        reserved=db.execute("SELECT COALESCE(SUM(quantity_milli),0) FROM core_stockreservation WHERE state IN ('pending','rejected')").fetchone()[0]
                        self.assertEqual(balance-reserved,2000)
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_stockalertevent").fetchone()[0],1)
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_recoverysaleholddecision").fetchone()[0],1 if mode=="stage-retry" else 2)
