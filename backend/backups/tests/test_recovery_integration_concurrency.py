"""Independent SQLite writers cannot apply recovered business operations twice."""
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

from backups.tests import test_recovery_sale_integration as fixtures
from identity.models import DeviceSession


class RecoveryIntegrationConcurrencyTests(TransactionTestCase):
    worker_action="integrate"
    race_modes=("same-command","concurrent-decisions")
    setUp=fixtures.RecoverySaleIntegrationTests.setUp
    authenticate=fixtures.RecoverySaleIntegrationTests.authenticate
    generation=fixtures.RecoverySaleIntegrationTests.generation
    record=fixtures.RecoverySaleIntegrationTests.record
    device=fixtures.RecoverySaleIntegrationTests.device
    review=fixtures.RecoverySaleIntegrationTests.review
    payload=fixtures.RecoverySaleIntegrationTests.payload
    url=fixtures.RecoverySaleIntegrationTests.url
    source=fixtures.RecoverySaleIntegrationTests.source
    capture=fixtures.RecoverySaleIntegrationTests.capture
    preview=fixtures.RecoverySaleIntegrationTests.preview

    def test_two_processes_apply_or_retry_with_one_set_of_business_effects(self):
        backend=Path(__file__).resolve().parents[2]
        capture=self.capture(self.source())
        preview=self.preview(capture)
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
from backups.services.recovery_collection_reviews import decide_collection_review
from common.services.errors import OperationConflict
data=json.loads(sys.stdin.read())
Path(sys.argv[3]).touch()
deadline=time.monotonic()+30
while not Path(sys.argv[4]).exists():
 if time.monotonic()>deadline:raise RuntimeError('worker barrier timed out')
 time.sleep(.01)
try:
 execute=decide_collection_review if data['action']=='review_collection' else integrate_recovery_operation
 receipt,created=execute(shop_id=data['shop'],actor=User.objects.get(id=data['actor']),
  session_id=data['session'],capture_id=data['capture'],data=data['request'])
 print(json.dumps({'created':created,'receipt':receipt}))
except OperationConflict:print(json.dumps({'conflict':True}))
"""
        with TemporaryDirectory(prefix="recovery-integration-race-") as directory:
            root=Path(directory)
            for mode in self.race_modes:
                with self.subTest(mode=mode):
                    database=root/(mode+".sqlite3")
                    with closing(sqlite3.connect(database)) as target:connection.connection.backup(target)
                    start=root/(mode+"-start")
                    ready=[root/f"{mode}-ready-{index}" for index in range(2)]
                    command={"operation_id":str(uuid.uuid4()),"previous_version":preview["version"],
                        "preview_token":preview["preview_token"],"reason":"Contrôle concurrent des faits"}
                    if self.worker_action=="review_collection":command.update(state="pending",previous_version=preview["review_version"])
                    def run(index):
                        request=dict(command)
                        if index and mode=="concurrent-decisions":request["operation_id"]=str(uuid.uuid4())
                        action=self.worker_action
                        if action=="mixed_collection":
                            action="review_collection" if index else "integrate"
                            if index:request.update(operation_id=str(uuid.uuid4()),state="refused",previous_version=preview["review_version"])
                        data={"action":action,"actor":str(self.owner.id),"shop":str(self.a.id),"session":str(session.id),
                            "capture":str(capture.id),"request":request}
                        result=subprocess.run([sys.executable,"-I","-X","utf8","-c",worker,str(backend),str(database),
                            str(ready[index]),str(start)],input=json.dumps(data),text=True,capture_output=True,timeout=60)
                        self.assertEqual(result.returncode,0,result.stderr)
                        return json.loads(result.stdout)
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        futures=[pool.submit(run,index) for index in range(2)]
                        deadline=time.monotonic()+30
                        while not all(path.exists() for path in ready) and time.monotonic()<deadline:time.sleep(.01)
                        ready_before_deadline=[path.exists() for path in ready]
                        start.touch()
                        results=[];errors=[]
                        for future in futures:
                            try:results.append(future.result())
                            except Exception as error:errors.append(repr(error))
                        self.assertEqual(errors,[],errors)
                        self.assertTrue(all(ready_before_deadline),ready_before_deadline)
                    self.assertEqual(sum(item.get("created",False) for item in results),1)
                    if mode=="same-command":self.assertEqual(results[0]["receipt"],results[1]["receipt"])
                    else:self.assertEqual(sum(item.get("conflict",False) for item in results),1)
                    with closing(sqlite3.connect(database)) as db:
                        self.assert_effects(db)

    def assert_effects(self,db):
        for table in ("core_sale","core_saleline","core_salepayment","core_cashmovement",
                      "backups_recoveredoperationorigin","backups_recoveryintegrationdecision","core_stockalertevent"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],1,table)
        self.assertEqual(db.execute("SELECT COUNT(*),SUM(quantity_milli) FROM core_stockmovement").fetchone(),(2,2000))
        self.assertEqual(db.execute("SELECT SUM(amount_fcfa) FROM core_cashmovement").fetchone()[0],500)


from backups.tests import test_recovery_collections as collection_fixtures


class RecoveryCollectionConcurrencyTests(RecoveryIntegrationConcurrencyTests):
    setUp=collection_fixtures.RecoveryCollectionTests.setUp
    source=collection_fixtures.RecoveryCollectionTests.source
    capture=collection_fixtures.RecoveryCollectionTests.capture

    def assert_effects(self,db):
        expected={"core_customerpayment":1,"core_customerpaymentpart":2,"core_debtallocation":2,
            "core_cashmovement":2,"core_customerledgerentry":1,"backups_recoveredoperationorigin":1,
            "backups_recoveryintegrationdecision":1,"core_sale":0,"core_stockmovement":0}
        for table,count in expected.items():
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],count,table)
        self.assertEqual(db.execute("SELECT SUM(amount_fcfa) FROM core_cashmovement").fetchone()[0],5000)
        self.assertEqual(db.execute("SELECT SUM(remaining_fcfa) FROM core_customerdebt").fetchone()[0],5000)


class RecoveryCollectionStagingConcurrencyTests(RecoveryCollectionConcurrencyTests):
    worker_action="review_collection"

    def assert_effects(self,db):
        for table in ("core_customerpayment","core_cashmovement","core_customerledgerentry","backups_recoveredoperationorigin",
                "backups_recoveryintegrationdecision","core_stockmovement"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],0,table)
        for table in ("backups_recoverycollectionreview","backups_recoverycollectionreviewdecision"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],1,table)
        self.assertEqual(db.execute("SELECT SUM(remaining_fcfa) FROM core_customerdebt").fetchone()[0],10000)


from backups.tests import test_recovery_collection_reviews as review_fixtures


class RecoveryCollectionApprovalConcurrencyTests(RecoveryCollectionConcurrencyTests):
    decide=review_fixtures.RecoveryCollectionReviewTests.decide
    stage=review_fixtures.RecoveryCollectionReviewTests.stage

    def capture(self,source):
        captured=super().capture(source)
        self.stage(captured)
        return captured

    def assert_effects(self,db):
        super().assert_effects(db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_recoverycollectionreview").fetchone()[0],1)
        self.assertEqual(db.execute("SELECT state,version FROM backups_recoverycollectionreview").fetchone(),("validated",2))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_recoverycollectionreviewdecision").fetchone()[0],2)


class RecoveryCollectionRefusalConcurrencyTests(RecoveryCollectionApprovalConcurrencyTests):
    worker_action="mixed_collection"
    race_modes=("approve-versus-refuse",)

    def assert_effects(self,db):
        state=db.execute("SELECT state,version FROM backups_recoverycollectionreview").fetchone()
        self.assertIn(state,(("validated",2),("refused",2)))
        if state[0]=="validated":
            super().assert_effects(db)
        else:
            for table in ("core_customerpayment","core_cashmovement","core_customerledgerentry",
                    "backups_recoveryintegrationdecision","backups_recoveredoperationorigin","core_stockmovement"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],0,table)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM backups_recoverycollectionreviewdecision").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT SUM(remaining_fcfa) FROM core_customerdebt").fetchone()[0],10000)


from backups.tests import test_recovery_customer_dependencies as customer_fixtures
from customers.models import Customer


class RecoveryCustomerCreationConcurrencyTests(RecoveryIntegrationConcurrencyTests):
    setUp=customer_fixtures.RecoveryCustomerDependencyTests.setUp
    source=customer_fixtures.RecoveryCustomerDependencyTests.customer_source
    capture=customer_fixtures.RecoveryCustomerDependencyTests.capture
    preview=customer_fixtures.RecoveryCustomerDependencyTests.preview

    def assert_effects(self,db):
        for table in ("core_customer","backups_recoveredoperationorigin","backups_recoveryintegrationdecision"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],1,table)
        for table in ("core_customerdebt","core_customerledgerentry","core_customerpayment","core_cashmovement","core_sale","core_stockmovement"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],0,table)
        self.assertEqual(db.execute("SELECT actor_id FROM core_activity WHERE action='client_created'").fetchone()[0],self.owner.id.hex)


class RecoveryOpeningDebtConcurrencyTests(RecoveryCustomerCreationConcurrencyTests):
    def source(self):
        customer=Customer.objects.create(shop=self.a,name="Client connu avant ouverture récupérée")
        return customer_fixtures.RecoveryCustomerDependencyTests.debt_source(self,customer.id)

    def assert_effects(self,db):
        for table in ("core_customer","core_customerdebt","core_customerledgerentry","backups_recoveredoperationorigin","backups_recoveryintegrationdecision"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],1,table)
        for table in ("core_customerpayment","core_cashmovement","core_sale","core_stockmovement"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],0,table)
        self.assertEqual(db.execute("SELECT amount_fcfa,remaining_fcfa FROM core_customerdebt").fetchone(),(8000,8000))
        self.assertEqual(db.execute("SELECT delta_fcfa,debt_delta_fcfa,credit_delta_fcfa FROM core_customerledgerentry").fetchone(),(8000,8000,0))


from backups.tests import test_recovery_customer_revisions as revision_fixtures


class RecoveryCustomerUpdateConcurrencyTests(RecoveryIntegrationConcurrencyTests):
    setUp=revision_fixtures.RecoveryCustomerRevisionTests.setUp
    before=revision_fixtures.RecoveryCustomerRevisionTests.before
    contact_source=revision_fixtures.RevisionSources.contact_source
    capture=revision_fixtures.RecoveryCustomerRevisionTests.capture
    preview=revision_fixtures.RecoveryCustomerRevisionTests.preview

    def source(self):return self.contact_source(self.before())

    def assert_effects(self,db):
        self.assertEqual(db.execute("SELECT name,version FROM core_customer").fetchone(),("Nom corrigé",2))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_activity WHERE action='client_updated'").fetchone()[0],1)
        for table in ("backups_recoveredoperationorigin","backups_recoveryintegrationdecision"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],1)
        for table in ("core_customerledgerentry","core_cashmovement","core_customerpayment","core_stockmovement","core_sale"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],0)
        self.assertEqual(db.execute("SELECT amount_fcfa,remaining_fcfa,due_version FROM core_customerdebt").fetchone(),(8000,5000,1))


class RecoveryDueChangeConcurrencyTests(RecoveryCustomerUpdateConcurrencyTests):
    deadline_source=revision_fixtures.RevisionSources.deadline_source

    def source(self):return self.deadline_source(self.customer.id,self.debt.id)

    def assert_effects(self,db):
        self.assertEqual(db.execute("SELECT name,version FROM core_customer").fetchone(),("Nom initial",1))
        self.assertEqual(db.execute("SELECT amount_fcfa,remaining_fcfa,due_version FROM core_customerdebt").fetchone(),(8000,5000,2))
        for table in ("backups_recoveredoperationorigin","backups_recoveryintegrationdecision","core_customerledgerentry"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],1)
        self.assertEqual(db.execute("SELECT delta_fcfa,debt_delta_fcfa,credit_delta_fcfa FROM core_customerledgerentry").fetchone(),(0,0,0))
        for table in ("core_cashmovement","core_customerpayment","core_stockmovement","core_sale"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],0)


from backups.tests import test_recovery_customer_refunds as refund_fixtures


class RecoveryPaymentRefundConcurrencyTests(RecoveryIntegrationConcurrencyTests):
    source_kind="payment"
    setUp=refund_fixtures.RecoveryCustomerRefundTests.setUp
    capture=refund_fixtures.RecoveryCustomerRefundTests.capture
    preview=refund_fixtures.RecoveryCustomerRefundTests.preview

    def source(self):return refund_fixtures.RefundSources.source(self,self.source_kind)

    def assert_effects(self,db):
        for table in ("core_paymentregularization","core_cashmovement","core_customerledgerentry",
                "backups_recoveredoperationorigin","backups_recoveryintegrationdecision"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],1,table)
        self.assertEqual(db.execute("SELECT amount_fcfa FROM core_cashmovement").fetchone()[0],-2000)
        self.assertEqual(db.execute("SELECT delta_fcfa,debt_delta_fcfa,credit_delta_fcfa FROM core_customerledgerentry").fetchone(),(2000,0,-2000))
        self.assertEqual(db.execute("SELECT unallocated_fcfa FROM core_customerpayment").fetchone()[0],3000 if self.source_kind=="payment" else 5000)
        self.assertEqual(db.execute("SELECT remaining_fcfa FROM core_customercredit").fetchone()[0],3000 if self.source_kind=="credit" else 5000)
        for table in ("core_stockmovement","core_sale","core_customerdebt"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],0,table)


class RecoverySaleCreditRefundConcurrencyTests(RecoveryPaymentRefundConcurrencyTests):
    source_kind="credit"


from backups.tests import test_recovery_customer_allocations as allocation_fixtures


class RecoveryPaymentAllocationConcurrencyTests(RecoveryIntegrationConcurrencyTests):
    source_kind="payment"
    setUp=allocation_fixtures.RecoveryCustomerAllocationTests.setUp
    capture=allocation_fixtures.RecoveryCustomerAllocationTests.capture
    preview=allocation_fixtures.RecoveryCustomerAllocationTests.preview

    def source(self):return allocation_fixtures.AllocationSources.source(self,self.source_kind)

    def assert_effects(self,db):
        for table in ("core_paymentregularization","core_customerledgerentry","backups_recoveredoperationorigin","backups_recoveryintegrationdecision"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],1,table)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_regularizationallocation").fetchone()[0],2)
        self.assertEqual(db.execute("SELECT delta_fcfa,debt_delta_fcfa,credit_delta_fcfa FROM core_customerledgerentry").fetchone(),(0,-2500,-2500))
        self.assertEqual(db.execute("SELECT remaining_fcfa FROM core_customerdebt ORDER BY amount_fcfa").fetchall(),[(2000,),(2500,)])
        self.assertEqual(db.execute("SELECT unallocated_fcfa FROM core_customerpayment").fetchone()[0],2500 if self.source_kind=="payment" else 5000)
        self.assertEqual(db.execute("SELECT remaining_fcfa FROM core_customercredit").fetchone()[0],2500 if self.source_kind=="credit" else 5000)
        for table in ("core_cashmovement","core_stockmovement","core_sale"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],0,table)


class RecoverySaleCreditAllocationConcurrencyTests(RecoveryPaymentAllocationConcurrencyTests):
    source_kind="credit"


from backups.tests import test_recovery_return_settlements as return_settlement_fixtures


class RecoveryReturnRefundConcurrencyTests(RecoveryIntegrationConcurrencyTests):
    settlement_kind="refund"
    anonymous=False
    setUp=return_settlement_fixtures.RecoveryReturnSettlementTests.setUp
    prepare_return=return_settlement_fixtures.RecoveryReturnSettlementTests.prepare_return
    capture=return_settlement_fixtures.RecoveryReturnSettlementTests.capture
    preview=return_settlement_fixtures.RecoveryReturnSettlementTests.preview

    def source(self):
        if self.anonymous:self.returned=self.prepare_return(anonymous=True)
        return return_settlement_fixtures.SettlementSources.source(self,self.settlement_kind)

    def assert_effects(self,db):
        for table in ("core_returncreditsettlement","backups_recoveredoperationorigin","backups_recoveryintegrationdecision"):
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],1,table)
        allocated=self.settlement_kind=="allocation"
        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_returncreditallocation").fetchone()[0],2 if allocated else 0)
        self.assertEqual(db.execute("SELECT SUM(amount_fcfa) FROM core_returncreditsettlement WHERE sale_return_id=?",[self.returned.id.hex]).fetchone()[0],2500 if allocated else 2000)
        self.assertEqual(db.execute("SELECT remaining_fcfa FROM core_customerdebt ORDER BY amount_fcfa").fetchall(),[(2000,),(2500,)] if allocated else [(3000,),(4000,)])
        sales=2 if self.anonymous else 1
        self.assertEqual(db.execute("SELECT COUNT(*),SUM(amount_fcfa) FROM core_cashmovement").fetchone(),
            (sales+(not allocated),5000*sales-(0 if allocated else 2000)))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_customerledgerentry").fetchone()[0],3+(not self.anonymous))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_stockmovement").fetchone()[0],sales+1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_returnreceipt").fetchone()[0],0)


class RecoveryReturnAllocationConcurrencyTests(RecoveryReturnRefundConcurrencyTests):
    settlement_kind="allocation"


class RecoveryAnonymousReturnRefundConcurrencyTests(RecoveryReturnRefundConcurrencyTests):
    anonymous=True


from backups.tests import test_recovery_sale_returns as commercial_return_fixtures


class RecoveryCommercialReturnConcurrencyTests(RecoveryIntegrationConcurrencyTests):
    credit=False
    setUp=commercial_return_fixtures.RecoverySaleReturnTests.setUp
    prepare_sale=commercial_return_fixtures.RecoverySaleReturnTests.prepare_sale
    capture=commercial_return_fixtures.RecoverySaleReturnTests.capture
    preview=commercial_return_fixtures.RecoverySaleReturnTests.preview

    def source(self):
        sale=self.prepare_sale(mode='credit',payments=[{'id':str(uuid.uuid4()),'method':'cash','amount_fcfa':2000}]) if self.credit else self.sale
        self.returned_sale=sale
        return commercial_return_fixtures.ReturnSources.source(self,sale)

    def assert_effects(self,db):
        for table in ('core_salereturn','core_salereturnline','backups_recoveredoperationorigin','backups_recoveryintegrationdecision'):
            self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],1,table)
        self.assertEqual(db.execute('SELECT amount_fcfa,debt_reduced_fcfa,credit_fcfa FROM core_salereturn').fetchone(),
            (5000,3000,2000) if self.credit else (5000,0,5000))
        self.assertEqual(db.execute('SELECT COUNT(*),SUM(amount_fcfa) FROM core_cashmovement').fetchone(),(2,7000) if self.credit else (1,5000))
        self.assertEqual(db.execute('SELECT COUNT(*),SUM(quantity_milli) FROM core_stockmovement').fetchone(),(3,30000) if self.credit else (2,40000))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM core_returnreceipt').fetchone()[0],0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM core_customerledgerentry').fetchone()[0],5 if self.credit else 3)
        self.assertEqual(db.execute('SELECT remaining_fcfa FROM core_customerdebt WHERE sale_id IS NULL ORDER BY amount_fcfa').fetchall(),[(3000,),(4000,)])
        if self.credit:
            self.assertEqual(db.execute('SELECT remaining_fcfa FROM core_customerdebt WHERE sale_id=?',
                [uuid.UUID(self.returned_sale['id']).hex]).fetchone()[0],0)


class RecoveryCreditCommercialReturnConcurrencyTests(RecoveryCommercialReturnConcurrencyTests):
    credit=True


from backups.tests import test_recovery_return_receipts as physical_receipt_fixtures


class RecoverySellableReceiptConcurrencyTests(RecoveryIntegrationConcurrencyTests):
    condition='sellable'
    setUp=physical_receipt_fixtures.RecoveryReturnReceiptTests.setUp
    prepare_sale=physical_receipt_fixtures.RecoveryReturnReceiptTests.prepare_sale
    prepare_return=physical_receipt_fixtures.RecoveryReturnReceiptTests.prepare_return
    capture=physical_receipt_fixtures.RecoveryReturnReceiptTests.capture
    preview=physical_receipt_fixtures.RecoveryReturnReceiptTests.preview

    def source(self):return physical_receipt_fixtures.ReceiptSources.source(self,self.condition)

    def assert_effects(self,db):
        for table in ('core_returnreceipt','core_returncostsnapshot','backups_recoveredoperationorigin','backups_recoveryintegrationdecision'):
            self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],1,table)
        self.assertEqual(db.execute('SELECT quantity_milli,condition FROM core_returnreceipt').fetchone(),(2000,self.condition))
        self.assertEqual(db.execute('SELECT COUNT(*),SUM(quantity_milli) FROM core_stockmovement').fetchone(),
            (3,42000) if self.condition=='sellable' else (2,40000))
        self.assertEqual(db.execute('SELECT COUNT(*),SUM(amount_fcfa) FROM core_cashmovement').fetchone(),(1,5000))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM core_customerledgerentry').fetchone()[0],3)
        self.assertEqual(db.execute('SELECT total_cost_nanos_fcfa FROM core_returncostsnapshot').fetchone()[0],'200000000000')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM core_salereturn').fetchone()[0],1)


class RecoveryUnsellableReceiptConcurrencyTests(RecoverySellableReceiptConcurrencyTests):
    condition='unsellable'


from backups.tests import test_recovery_unsellable_decisions as disposition_fixtures


class RecoveryRestockDispositionConcurrencyTests(RecoveryIntegrationConcurrencyTests):
    disposition_kind='restock'
    setUp=disposition_fixtures.RecoveryUnsellableDecisionTests.setUp
    prepare_sale=disposition_fixtures.RecoveryUnsellableDecisionTests.prepare_sale
    prepare_return=disposition_fixtures.RecoveryUnsellableDecisionTests.prepare_return
    receipt_url=disposition_fixtures.RecoveryUnsellableDecisionTests.receipt_url
    capture=disposition_fixtures.RecoveryUnsellableDecisionTests.capture
    preview=disposition_fixtures.RecoveryUnsellableDecisionTests.preview

    def source(self):return disposition_fixtures.DecisionSources.source(self,self.disposition_kind)

    def assert_effects(self,db):
        for table in ('core_nonsellabledecision','backups_recoveredoperationorigin','backups_recoveryintegrationdecision'):
            self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],1,table)
        self.assertEqual(db.execute('SELECT quantity_milli,after_quantity_milli,kind FROM core_nonsellabledecision').fetchone(),(1000,5000,self.disposition_kind))
        self.assertEqual(db.execute('SELECT COUNT(*),SUM(quantity_milli) FROM core_stockmovement').fetchone(),
            (3,41000) if self.disposition_kind=='restock' else (2,40000))
        self.assertEqual(db.execute('SELECT COUNT(*),SUM(amount_fcfa) FROM core_cashmovement').fetchone(),(1,5000))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM core_customerledgerentry').fetchone()[0],3)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM core_returnreceipt').fetchone()[0],1)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM core_supplierledgerentry').fetchone()[0],1 if self.disposition_kind=='supplier_return' else 0)
        for table in ('core_supplierdebt','core_suppliercredit','core_supplierreturn','core_stockloss'):
            self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],0,table)


class RecoverySupplierDispositionConcurrencyTests(RecoveryRestockDispositionConcurrencyTests):
    disposition_kind='supplier_return'


class RecoveryScrapDispositionConcurrencyTests(RecoveryRestockDispositionConcurrencyTests):
    disposition_kind='scrap'


from backups.tests import test_recovery_sale_cancellations as cancellation_fixtures


class RecoveryCancellationConcurrencyTests(RecoveryIntegrationConcurrencyTests):
    credit=False
    no_child=False
    setUp=cancellation_fixtures.RecoverySaleCancellationTests.setUp
    prepare_sale=cancellation_fixtures.RecoverySaleCancellationTests.prepare_sale
    capture=cancellation_fixtures.RecoverySaleCancellationTests.capture
    preview=cancellation_fixtures.RecoverySaleCancellationTests.preview

    def source(self):
        sale=self.prepare_sale(mode='credit',payments=[{'id':str(uuid.uuid4()),'method':'cash','amount_fcfa':2000}]) if self.credit else self.sale
        self.cancelled_sale=sale
        if self.no_child:
            previous=commercial_return_fixtures.ReturnSources.source(self,sale)
            raw={key:value for key,value in previous.payload['original_request'].items() if key not in ('sale_id','installation_id')}
            response=self.client.post(f"/api/v1/shops/{self.a.id}/sales/{sale['id']}/returns/",raw,format='json')
            self.assertEqual(response.status_code,201,response.content)
        source=cancellation_fixtures.CancellationSources.source(self,sale,with_child=not self.no_child)
        if source.child:self.capture(source.child)
        return source

    def assert_effects(self,db):
        for table in ('core_salecancellation','core_salereturn','core_salereturnline'):
            self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],1,table)
        for table in ('backups_recoveredoperationorigin','backups_recoveryintegrationdecision'):
            self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],1 if self.no_child else 2,table)
        self.assertEqual(db.execute('SELECT amount_fcfa,debt_reduced_fcfa,credit_fcfa FROM core_salereturn').fetchone(),
            (5000,3000,2000) if self.credit else (5000,0,5000))
        self.assertEqual(db.execute('SELECT COUNT(*),SUM(amount_fcfa) FROM core_cashmovement').fetchone(),(2,7000) if self.credit else (1,5000))
        self.assertEqual(db.execute('SELECT COUNT(*),SUM(quantity_milli) FROM core_stockmovement').fetchone(),(3,30000) if self.credit else (2,40000))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM core_returnreceipt').fetchone()[0],0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM core_customerledgerentry').fetchone()[0],5 if self.credit else 3)
        self.assertEqual(db.execute('SELECT remaining_fcfa FROM core_customerdebt WHERE sale_id IS NULL ORDER BY amount_fcfa').fetchall(),[(3000,),(4000,)])
        if self.credit:
            self.assertEqual(db.execute('SELECT remaining_fcfa FROM core_customerdebt WHERE sale_id=?',[uuid.UUID(self.cancelled_sale['id']).hex]).fetchone()[0],0)


class RecoveryCreditCancellationConcurrencyTests(RecoveryCancellationConcurrencyTests):
    credit=True


class RecoveryFullyReturnedCancellationConcurrencyTests(RecoveryCancellationConcurrencyTests):
    no_child=True


from backups.tests import test_recovery_supplier_dependencies as supplier_fixtures


class RecoverySupplierCreationConcurrencyTests(RecoveryIntegrationConcurrencyTests):
    source_kind='creation'
    setUp=supplier_fixtures.RecoverySupplierDependencyTests.setUp
    capture=supplier_fixtures.RecoverySupplierDependencyTests.capture
    preview=supplier_fixtures.RecoverySupplierDependencyTests.preview
    native_supplier=supplier_fixtures.RecoverySupplierDependencyTests.native_supplier
    native_url=supplier_fixtures.RecoverySupplierDependencyTests.native_url
    native_body=supplier_fixtures.RecoverySupplierDependencyTests.native_body
    supplier_source=supplier_fixtures.SupplierSources.supplier_source
    opening_source=supplier_fixtures.SupplierSources.opening_source

    def source(self):
        return self.supplier_source() if self.source_kind=='creation' else self.opening_source(self.native_supplier(),self.source_kind)

    def assert_effects(self,db):
        debt=self.source_kind=='debt';balance=self.source_kind in ('advance','credit')
        for table,count in (('core_supplier',1),('core_supplierdebt',int(debt)),('core_suppliercredit',int(balance)),
                ('core_supplierledgerentry',int(debt or balance)),('backups_recoveredoperationorigin',1),('backups_recoveryintegrationdecision',1),
                ('core_cashmovement',0),('core_stockmovement',0),('core_purchasedocument',0),('core_supplierpayment',0),('core_supplierreturn',0)):
            self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],count,table)
        if debt:self.assertEqual(db.execute('SELECT amount_fcfa,remaining_fcfa FROM core_supplierdebt').fetchone(),(8000,8000))
        if balance:self.assertEqual(db.execute('SELECT kind,amount_fcfa,remaining_fcfa FROM core_suppliercredit').fetchone(),
            ('opening_advance',3000,3000) if self.source_kind=='advance' else ('opening_credit',2000,2000))
        if debt or balance:
            self.assertEqual(db.execute('SELECT delta_fcfa,debt_delta_fcfa,advance_delta_fcfa,credit_delta_fcfa FROM core_supplierledgerentry').fetchone(),
                (8000,8000,0,0) if debt else (-3000,0,3000,0) if self.source_kind=='advance' else (-2000,0,0,2000))


class RecoverySupplierDebtConcurrencyTests(RecoverySupplierCreationConcurrencyTests):
    source_kind='debt'


class RecoverySupplierAdvanceConcurrencyTests(RecoverySupplierCreationConcurrencyTests):
    source_kind='advance'


class RecoverySupplierCreditConcurrencyTests(RecoverySupplierCreationConcurrencyTests):
    source_kind='credit'


from backups.tests import test_recovery_supplier_payments as supplier_payment_fixtures


class RecoverySupplierPaymentConcurrencyTests(RecoveryIntegrationConcurrencyTests):
    allocated=False
    setUp=supplier_payment_fixtures.RecoverySupplierPaymentTests.setUp
    capture=supplier_payment_fixtures.RecoverySupplierPaymentTests.capture
    preview=supplier_payment_fixtures.RecoverySupplierPaymentTests.preview
    native_supplier=supplier_payment_fixtures.RecoverySupplierPaymentTests.native_supplier
    native_opening=supplier_payment_fixtures.RecoverySupplierPaymentTests.native_opening
    native_body=supplier_payment_fixtures.RecoverySupplierPaymentTests.native_body
    native_url=supplier_payment_fixtures.RecoverySupplierPaymentTests.native_url
    supplier_source=supplier_payment_fixtures.RecoverySupplierPaymentTests.supplier_source
    opening_source=supplier_payment_fixtures.RecoverySupplierPaymentTests.opening_source
    payment_source=supplier_payment_fixtures.PaymentSources.payment_source
    allocation_source=supplier_payment_fixtures.PaymentSources.allocation_source
    native_payment=supplier_payment_fixtures.RecoverySupplierPaymentTests.native_payment
    pay_path=supplier_payment_fixtures.RecoverySupplierPaymentTests.pay_path

    def source(self):
        return self.allocation_source(self.native_payment()[1]['id']) if self.allocated else self.payment_source()

    def assert_effects(self,db):
        for table,count in (('core_supplierpayment',1),('core_supplierpaymentpart',2),('core_supplierpaymentallocation',2 if self.allocated else 0),
                ('core_supplierledgerentry',4 if self.allocated else 3),('backups_recoveredoperationorigin',1),('backups_recoveryintegrationdecision',1),
                ('core_purchasedocument',0),('core_stockmovement',0)):
            self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],count,table)
        self.assertEqual(db.execute('SELECT COUNT(*),SUM(amount_fcfa) FROM core_cashmovement').fetchone(),(2,-10000))
        self.assertEqual(db.execute('SELECT amount_fcfa,unallocated_fcfa FROM core_supplierpayment').fetchone(),(10000,3000 if self.allocated else 10000))
        self.assertEqual(sorted(row[0] for row in db.execute('SELECT remaining_fcfa FROM core_supplierdebt')), [4000,5000] if self.allocated else [8000,8000])


class RecoverySupplierPaymentAllocationConcurrencyTests(RecoverySupplierPaymentConcurrencyTests):
    allocated=True


from backups.tests import test_recovery_purchases as purchase_fixtures


class RecoveryPurchaseOrderConcurrencyTests(RecoveryIntegrationConcurrencyTests):
    document_kind='order'
    setUp=purchase_fixtures.RecoveryPurchaseTests.setUp
    capture=purchase_fixtures.RecoveryPurchaseTests.capture
    preview=purchase_fixtures.RecoveryPurchaseTests.preview
    native_supplier=purchase_fixtures.RecoveryPurchaseTests.native_supplier
    native_url=purchase_fixtures.RecoveryPurchaseTests.native_url
    native_body=purchase_fixtures.RecoveryPurchaseTests.native_body
    supplier_source=purchase_fixtures.RecoveryPurchaseTests.supplier_source
    purchase_source=purchase_fixtures.PurchaseSources.purchase_source

    def source(self):
        source=self.purchase_source(self.document_kind)
        self.source_snapshot=source.payload['purchase']
        return source

    def assert_effects(self,db):
        invoice=self.document_kind=='invoice'
        for table,count in (('core_supplier',1),('core_purchasedocument',1),('core_purchaseline',1),
                ('core_supplierdebt',int(invoice)),('core_supplierledgerentry',int(invoice)),
                ('backups_recoveredoperationorigin',1),('backups_recoveryintegrationdecision',1),
                ('core_purchasereceipt',0),('core_stockmovement',0),('core_cashmovement',0),('core_supplierpayment',0)):
            self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],count,table)
        self.assertEqual(db.execute('SELECT kind,total_fcfa FROM core_purchasedocument').fetchone(),(self.document_kind,10000))
        self.assertEqual(db.execute('SELECT quantity_milli,factor_milli,base_quantity_milli,unit_price_fcfa,total_fcfa FROM core_purchaseline').fetchone(),
            (10000,24000,240000,1000,10000))
        if invoice:
            self.assertEqual(db.execute('SELECT id,amount_fcfa,remaining_fcfa FROM core_supplierdebt').fetchone(),
                (uuid.UUID(self.source_snapshot['debt']['id']).hex,10000,10000))
            self.assertEqual(db.execute('SELECT debt_delta_fcfa,advance_delta_fcfa,credit_delta_fcfa FROM core_supplierledgerentry').fetchone(),(10000,0,0))


class RecoveryPurchaseInvoiceConcurrencyTests(RecoveryPurchaseOrderConcurrencyTests):
    document_kind='invoice'
