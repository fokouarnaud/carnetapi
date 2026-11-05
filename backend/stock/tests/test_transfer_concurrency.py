"""Two SQLite writers cannot receive the same physical transit quantity twice."""
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
from identity.models import DeviceSession
from stock.tests import test_transfers as fixtures


class TransferConcurrencyTests(TransactionTestCase):
    setUp=fixtures.TransferApiTests.setUp
    product_at=fixtures.TransferApiTests.product_at
    authenticate=fixtures.TransferApiTests.authenticate
    url=fixtures.TransferApiTests.url
    shipment=fixtures.TransferApiTests.shipment
    depart=fixtures.TransferApiTests.depart
    arrival=fixtures.TransferApiTests.arrival

    def test_retries_and_competing_receipts_conserve_one_receipt_and_remaining_transit(self):
        transfer,_=self.depart()
        self.client=self.authenticate(self.destination_manager)
        session=DeviceSession.objects.get(installation__user=self.destination_manager)
        command=self.arrival(transfer)
        backend=Path(__file__).resolve().parents[2]
        worker="""
import json,os,sys,time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
os.environ['DJANGO_SETTINGS_MODULE']='config.test_settings'
os.environ['APP_DB_PATH']=sys.argv[2]
import django
django.setup()
from identity.models import User,DeviceSession
from stock.api.transfers import ArrivalInput
from stock.services.transfers import receive_transfer
from common.services.errors import OperationConflict
body=json.loads(sys.stdin.read())
request=SimpleNamespace(user=User.objects.get(id=body['actor']),device_session=DeviceSession.objects.select_related('installation').get(id=body['session']))
incoming=ArrivalInput(data=body['command']);incoming.is_valid(raise_exception=True)
Path(sys.argv[3]).touch()
deadline=time.monotonic()+30
while not Path(sys.argv[4]).exists():
 if time.monotonic()>deadline:raise RuntimeError('transfer barrier timeout')
 time.sleep(.01)
try:
 receipt,created=receive_transfer(body['shop'],body['transfer'],request,incoming.validated_data)
 print(json.dumps({'created':created,'receipt':receipt}))
except OperationConflict:print(json.dumps({'conflict':True}))
"""
        with TemporaryDirectory(prefix="transfer-race-") as directory:
            root=Path(directory)
            for mode in ("retry","competing"):
                with self.subTest(mode=mode):
                    database=root/(mode+".sqlite3")
                    with closing(sqlite3.connect(database)) as target:connection.connection.backup(target)
                    start=root/(mode+"-start")
                    ready=[root/f"{mode}-ready-{index}" for index in range(2)]
                    def run(index):
                        data=json.loads(json.dumps(command))
                        if mode=="competing" and index:
                            data.update(id=str(uuid.uuid4()),operation_id=str(uuid.uuid4()))
                            data["lines"][0]["id"]=str(uuid.uuid4())
                        body={"shop":str(self.destination.id),"transfer":transfer["id"],"actor":str(self.destination_manager.id),
                            "session":str(session.id),"command":data}
                        result=subprocess.run([sys.executable,"-I","-X","utf8","-c",worker,str(backend),str(database),
                            str(ready[index]),str(start)],input=json.dumps(body),text=True,capture_output=True,timeout=60)
                        self.assertEqual(result.returncode,0,result.stderr)
                        return json.loads(result.stdout)
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        futures=[pool.submit(run,index) for index in range(2)]
                        deadline=time.monotonic()+30
                        while not all(path.exists() for path in ready) and time.monotonic()<deadline:time.sleep(.01)
                        start.touch()
                        self.assertTrue(all(path.exists() for path in ready))
                        results=[future.result() for future in futures]
                    self.assertEqual(sum(row.get("created",False) for row in results),1)
                    if mode=="retry":self.assertEqual(results[0]["receipt"],results[1]["receipt"])
                    else:self.assertEqual(sum(row.get("conflict",False) for row in results),1)
                    with closing(sqlite3.connect(database)) as db:
                        self.assertEqual(db.execute("SELECT COUNT(*),SUM(quantity_milli) FROM stock_stocktransferreceiptline").fetchone(),(1,3000))
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_stockmovement").fetchone()[0],3)
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_cashmovement").fetchone()[0],0)
