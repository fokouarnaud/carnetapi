"""Two real SQLite writers cannot receive a purchase remainder twice."""
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

from backups.tests import test_recovery_purchase_receipts as fixtures
from identity.models import DeviceSession


class RecoveryPurchaseReceiptConcurrencyTests(TransactionTestCase):
    setUp=fixtures.RecoveryPurchaseReceiptTests.setUp
    authenticate=fixtures.RecoveryPurchaseReceiptTests.authenticate
    generation=fixtures.RecoveryPurchaseReceiptTests.generation
    record=fixtures.RecoveryPurchaseReceiptTests.record
    device=fixtures.RecoveryPurchaseReceiptTests.device
    review=fixtures.RecoveryPurchaseReceiptTests.review
    payload=fixtures.RecoveryPurchaseReceiptTests.payload
    url=fixtures.RecoveryPurchaseReceiptTests.url
    capture=fixtures.RecoveryPurchaseReceiptTests.capture
    preview=fixtures.RecoveryPurchaseReceiptTests.preview
    native_url=fixtures.RecoveryPurchaseReceiptTests.native_url
    native_body=fixtures.RecoveryPurchaseReceiptTests.native_body
    native_supplier=fixtures.RecoveryPurchaseReceiptTests.native_supplier
    supplier_source=fixtures.RecoveryPurchaseReceiptTests.supplier_source
    purchase_source=fixtures.RecoveryPurchaseReceiptTests.purchase_source
    purchase_url=fixtures.RecoveryPurchaseReceiptTests.purchase_url
    native_purchase=fixtures.RecoveryPurchaseReceiptTests.native_purchase
    receipt_source=fixtures.RecoveryPurchaseReceiptTests.receipt_source

    def test_identical_retries_decisions_and_two_six_of_ten_arrivals_have_one_effect(self):
        backend=Path(__file__).resolve().parents[2]
        _,invoice=self.native_purchase();first=self.capture(self.receipt_source(invoice));second=self.capture(self.receipt_source(invoice))
        previews={item.id:self.preview(item) for item in (first,second)}
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
from backups.services.recovery_integration import integrate_recovery_operation
from common.services.errors import OperationConflict
data=json.loads(sys.stdin.read())
Path(sys.argv[3]).touch()
deadline=time.monotonic()+30
while not Path(sys.argv[4]).exists():
 if time.monotonic()>deadline:raise RuntimeError('worker barrier timed out')
 time.sleep(.01)
try:
 receipt,created=integrate_recovery_operation(shop_id=data['shop'],actor=User.objects.get(id=data['actor']),
  session_id=data['session'],capture_id=data['capture'],data=data['request'])
 print(json.dumps({'created':created,'receipt':receipt}))
except OperationConflict:print(json.dumps({'conflict':True}))
"""
        with TemporaryDirectory(prefix='purchase-receipt-race-',dir=backend.parent/'analysis') as directory:
            root=Path(directory)
            for mode in ('same-command','concurrent-decisions','distinct-receipts'):
                with self.subTest(mode=mode):
                    database=root/(mode+'.sqlite3')
                    with closing(sqlite3.connect(database)) as target:connection.connection.backup(target)
                    start=root/(mode+'-start');ready=[root/f'{mode}-ready-{index}' for index in range(2)]
                    operation=uuid.uuid4()
                    def run(index):
                        capture=second if index and mode=='distinct-receipts' else first;preview=previews[capture.id]
                        command={'operation_id':str(uuid.uuid4() if index and mode!='same-command' else operation),
                            'previous_version':preview['version'],'preview_token':preview['preview_token'],'reason':'Arrivée vérifiée en concurrence'}
                        data={'actor':str(self.owner.id),'shop':str(self.a.id),'session':str(session.id),'capture':str(capture.id),'request':command}
                        result=subprocess.run([sys.executable,'-I','-X','utf8','-c',worker,str(backend),str(database),str(ready[index]),str(start)],
                            input=json.dumps(data),text=True,capture_output=True,timeout=60)
                        self.assertEqual(result.returncode,0,result.stderr)
                        return json.loads(result.stdout)
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        futures=[pool.submit(run,index) for index in range(2)]
                        deadline=time.monotonic()+30
                        while not all(path.exists() for path in ready) and time.monotonic()<deadline:time.sleep(.01)
                        ready_in_time=[path.exists() for path in ready];start.touch()
                        results=[future.result() for future in futures]
                        self.assertTrue(all(ready_in_time),ready_in_time)
                    self.assertEqual(sum(item.get('created',False) for item in results),1)
                    if mode=='same-command':self.assertEqual(results[0]['receipt'],results[1]['receipt'])
                    else:self.assertEqual(sum(item.get('conflict',False) for item in results),1)
                    with closing(sqlite3.connect(database)) as db:
                        for table,count in (('core_purchasereceipt',1),('core_purchasereceiptline',1),('core_stockmovement',1),
                                ('core_supplierdebt',1),('core_supplierledgerentry',1),('core_cashmovement',0),
                                ('backups_recoveredoperationorigin',1),('backups_recoveryintegrationdecision',1)):
                            self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],count,table)
                        self.assertEqual(db.execute('SELECT SUM(quantity_milli) FROM core_stockmovement').fetchone()[0],144000)
                        self.assertEqual(db.execute('SELECT amount_fcfa,remaining_fcfa FROM core_supplierdebt').fetchone(),(10000,10000))
