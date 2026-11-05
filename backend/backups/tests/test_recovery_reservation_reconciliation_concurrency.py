"""Real independent SQLite writers cannot spend a physical-loss quantity twice."""
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
from backups.tests import test_recovery_reservation_reconciliation as fixtures
from identity.models import DeviceSession


class RecoveryReconciliationConcurrencyTests(TransactionTestCase):
    setUp=fixtures.RecoveryReservationReconciliationTests.setUp
    authenticate=fixtures.RecoveryReservationReconciliationTests.authenticate
    generation=fixtures.RecoveryReservationReconciliationTests.generation
    record=fixtures.RecoveryReservationReconciliationTests.record
    device=fixtures.RecoveryReservationReconciliationTests.device
    review=fixtures.RecoveryReservationReconciliationTests.review
    payload=fixtures.RecoveryReservationReconciliationTests.payload
    url=fixtures.RecoveryReservationReconciliationTests.url
    source=fixtures.RecoveryReservationReconciliationTests.source
    capture=fixtures.RecoveryReservationReconciliationTests.capture
    preview=fixtures.RecoveryReservationReconciliationTests.preview
    decide=fixtures.RecoveryReservationReconciliationTests.decide
    stage=fixtures.RecoveryReservationReconciliationTests.stage
    refused=fixtures.RecoveryReservationReconciliationTests.refused
    loss=fixtures.RecoveryReservationReconciliationTests.loss
    reconciliation_url=fixtures.RecoveryReservationReconciliationTests.reconciliation_url
    reconciliation_preview=fixtures.RecoveryReservationReconciliationTests.reconciliation_preview

    def test_retry_and_competing_reservations_keep_one_reconciliation_and_no_extra_movement(self):
        first,r1=self.refused();second,r2=self.refused();movement=self.loss()
        session=DeviceSession.objects.get(installation__user=self.owner)
        pairs=[(first,r1),(second,r2)]
        previews=[self.reconciliation_preview(capture,reservation,movement) for capture,reservation in pairs]
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
from backups.services.recovery_reservation_reconciliation import reconcile_reservation
from common.services.errors import OperationConflict
data=json.loads(sys.stdin.read())
Path(sys.argv[3]).touch()
deadline=time.monotonic()+30
while not Path(sys.argv[4]).exists():
 if time.monotonic()>deadline:raise RuntimeError('worker barrier timed out')
 time.sleep(.01)
try:
 receipt,created=reconcile_reservation(shop_id=data['shop'],actor=User.objects.get(id=data['actor']),
  session_id=data['session'],capture_id=data['capture'],reservation_id=data['reservation'],data=data['request'])
 print(json.dumps({'created':created,'receipt':receipt}))
except OperationConflict:print(json.dumps({'conflict':True}))
"""
        with TemporaryDirectory(prefix="reconciliation-race-") as directory:
            root=Path(directory)
            for mode in ("retry","competing-sources"):
                with self.subTest(mode=mode):
                    database=root/(mode+".sqlite3")
                    with closing(sqlite3.connect(database)) as target:connection.connection.backup(target)
                    start=root/(mode+"-start")
                    ready=[root/f"{mode}-ready-{index}" for index in range(2)]
                    command={"operation_id":str(uuid.uuid4()),"movement_id":str(movement.id),"quantity_milli":1000,
                        "preview_token":previews[0]["preview_token"],"reason":"Départ identique contrôlé"}
                    def run(index):
                        which=index if mode=="competing-sources" else 0
                        capture,reservation=pairs[which]
                        request=dict(command)
                        if which:request.update(operation_id=str(uuid.uuid4()),preview_token=previews[which]["preview_token"])
                        data={"shop":str(self.a.id),"actor":str(self.owner.id),"session":str(session.id),
                            "capture":str(capture.id),"reservation":str(reservation.id),"request":request}
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
                    self.assertEqual(sum(result.get("created",False) for result in results),1)
                    if mode=="retry":self.assertEqual(results[0]["receipt"],results[1]["receipt"])
                    else:self.assertEqual(sum(result.get("conflict",False) for result in results),1)
                    with closing(sqlite3.connect(database)) as db:
                        self.assertEqual(db.execute("SELECT COUNT(*), SUM(quantity_milli) FROM stock_stockreservationreconciliation").fetchone(),(1,1000))
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_stockmovement").fetchone()[0],2)
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_cashmovement").fetchone()[0],0)
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_sale").fetchone()[0],0)
