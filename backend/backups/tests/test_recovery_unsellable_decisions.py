"""Recovered dispositions consume the non-sellable pool without fictitious financial effects."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import uuid
from django.db import connection,IntegrityError
from django.test import TestCase,SimpleTestCase
from backups import models as backups
from backups.tests import test_recovery_return_receipts as fixtures
from common.models import Activity
from cash.models import CashMovement
from customers.models import CustomerLedgerEntry
from purchasing.models import Supplier,SupplierLedgerEntry,SupplierDebt,SupplierCredit,SupplierReturn
from stock.models import NonSellableDecision,ReturnReceipt,StockMovement,StockLoss
from stock.services.moving_average_cost import returned_cost_nanos
from stock.services.unsellable_balance import remaining_unsellable


class DecisionSources:
    def source(self,kind='restock',quantity=1000,*,before=6000):
        operation,actor,device,decision=[uuid.uuid4() for _ in range(4)]
        occurred=datetime(2026,10,25,13,tzinfo=timezone.utc)
        cost=deepcopy(self.receipt_data['cost'])
        if cost is not None:
            full=cost['total_cost_nanos_fcfa']
            cost['total_cost_nanos_fcfa']=None if full is None else str(returned_cost_nanos(int(full),self.received.quantity_milli,
                self.received.quantity_milli-before,quantity))
        supplier_id=str(self.supplier.id) if kind=='supplier_return' else None
        raw={'id':str(decision),'operation_id':str(operation),'receipt_id':str(self.received.id),'installation_id':str(device),
            'occurred_at':occurred.isoformat(),'reason':'Disposition physique vérifiée','kind':kind,'quantity_milli':quantity,
            'is_sellable_confirmed':kind=='restock','supplier_id':supplier_id}
        snapshot={'id':str(decision),'receipt_id':str(self.received.id),'sale_id':self.sale['id'],'sale_return_id':self.returned['id'],
            'return_line_id':str(self.parent_line.id),'product_id':str(self.product.id),'unit_kind':self.parent_line.unit_kind,
            'kind':kind,'quantity_milli':quantity,'after_quantity_milli':before-quantity,'is_sellable_confirmed':kind=='restock',
            'supplier_id':supplier_id,'occurred_at':occurred.isoformat(),'created_at':(occurred+timedelta(seconds=5)).isoformat(),
            'movement_id':str(uuid.uuid5(decision,'unsellable-restock')) if kind=='restock' else None,
            'unit_cost_micros':self.receipt_data['unit_cost_micros'],'cost_is_provisional':self.receipt_data['cost_is_provisional'],'cost':cost}
        return SimpleNamespace(operation_type='non_sellable_decision',operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=occurred,payload={'schema_version':1,'original_request':raw,'decision':snapshot})


class RecoveryDispositionSnapshotTests(SimpleTestCase,DecisionSources):
    def setUp(self):
        self.sale={'id':str(uuid.uuid4())};self.returned={'id':str(uuid.uuid4())};self.product=SimpleNamespace(id=uuid.uuid4())
        self.parent_line=SimpleNamespace(id=uuid.uuid4(),unit_kind='piece');self.received=SimpleNamespace(id=uuid.uuid4(),quantity_milli=6000)
        self.supplier=SimpleNamespace(id=uuid.uuid4());self.receipt_data={'unit_cost_micros':100000000,'cost_is_provisional':False,
            'cost':{'receipt_cost_id':str(uuid.uuid4()),'revision_id':None,'version':1,'origin_kind':'frozen','sale_cost_id':str(uuid.uuid4()),
                'cost_state':'known','total_cost_nanos_fcfa':'600000000000','unit_cost_micros_numerator':'100000000','unit_cost_micros_denominator':'1'}}

    def test_disposition_identity_confirmation_projection_and_exact_cost_are_validated(self):
        from backups.services.recovery_unsellable_decisions import decode_unsellable_decision,RecoveryDispositionInvalid
        for kind in ('restock','supplier_return','scrap'):
            self.assertEqual(decode_unsellable_decision(self.source(kind)).snapshot['kind'],kind)
        for case in ('quantity','piece','after','confirm','supplier','movement','projection','cost','version','revision','origin','time','unexpected'):
            with self.subTest(case=case):
                source=self.source();snapshot=source.payload['decision'];raw=source.payload['original_request']
                if case=='quantity':snapshot['quantity_milli']=2000
                elif case=='piece':snapshot['quantity_milli']=raw['quantity_milli']=500
                elif case=='after':snapshot['after_quantity_milli']=-1
                elif case=='confirm':snapshot['is_sellable_confirmed']=raw['is_sellable_confirmed']=False
                elif case=='supplier':snapshot['supplier_id']=raw['supplier_id']=str(self.supplier.id)
                elif case=='movement':snapshot['movement_id']=str(uuid.uuid4())
                elif case=='projection':snapshot['unit_cost_micros']=0
                elif case=='cost':snapshot['cost']['cost_state']='unknown'
                elif case=='version':snapshot['cost']['version']=0
                elif case=='revision':snapshot['cost']['revision_id']=str(uuid.uuid4())
                elif case=='origin':raw['installation_id']=str(uuid.uuid4())
                elif case=='time':snapshot['occurred_at']='2026-10-25T14:00:00Z'
                else:snapshot['refund_fcfa']=100
                with self.assertRaises(RecoveryDispositionInvalid):decode_unsellable_decision(source)

    def test_original_request_keeps_native_boolean_normalization_without_relaxing_frozen_confirmation(self):
        from backups.services.recovery_unsellable_decisions import decode_unsellable_decision,RecoveryDispositionInvalid
        source=self.source();source.payload['original_request']['is_sellable_confirmed']='true'
        self.assertIs(decode_unsellable_decision(source).request['is_sellable_confirmed'],True)
        source.payload['decision']['is_sellable_confirmed']='true'
        with self.assertRaises(RecoveryDispositionInvalid):decode_unsellable_decision(source)


class RecoveryUnsellableDecisionTests(TestCase,DecisionSources):
    authenticate=fixtures.RecoveryReturnReceiptTests.authenticate
    generation=fixtures.RecoveryReturnReceiptTests.generation
    record=fixtures.RecoveryReturnReceiptTests.record
    device=fixtures.RecoveryReturnReceiptTests.device
    review=fixtures.RecoveryReturnReceiptTests.review
    payload=fixtures.RecoveryReturnReceiptTests.payload
    url=fixtures.RecoveryReturnReceiptTests.url
    capture=fixtures.RecoveryReturnReceiptTests.capture
    preview=fixtures.RecoveryReturnReceiptTests.preview
    apply=fixtures.RecoveryReturnReceiptTests.apply
    prepare_sale=fixtures.RecoveryReturnReceiptTests.prepare_sale
    prepare_return=fixtures.RecoveryReturnReceiptTests.prepare_return
    receipt_url=fixtures.RecoveryReturnReceiptTests.native_url

    def setUp(self):
        fixtures.RecoveryReturnReceiptTests.setUp(self)
        source=fixtures.ReceiptSources.source(self,'unsellable',6000)
        body={key:value for key,value in source.payload['original_request'].items() if key not in ('return_line_id','installation_id')}
        response=self.client.post(self.receipt_url(),body,format='json');self.assertEqual(response.status_code,201,response.content)
        self.receipt_data=response.data;self.received=ReturnReceipt.objects.get(id=response.data['id'])
        self.supplier=Supplier.objects.create(shop=self.a,name='Fournisseur du pool non-vendable')

    def native_url(self):return self.receipt_url()+str(self.received.id)+'/decisions/'

    def native_body(self,source):
        return {key:value for key,value in source.payload['original_request'].items() if key not in ('receipt_id','installation_id')}

    def prepare_pool(self,sale,quantity=6000):
        self.prepare_return(sale)
        source=fixtures.ReceiptSources.source(self,'unsellable',quantity)
        body={key:value for key,value in source.payload['original_request'].items() if key not in ('return_line_id','installation_id')}
        response=self.client.post(self.receipt_url(),body,format='json');self.assertEqual(response.status_code,201,response.content)
        self.receipt_data=response.data;self.received=ReturnReceipt.objects.get(id=response.data['id'])

    def test_three_dispositions_consume_pool_once_with_only_restock_available_and_no_money(self):
        cash=list(CashMovement.objects.values_list('amount_fcfa',flat=True));ledger=CustomerLedgerEntry.objects.count()
        for index,kind in enumerate(('restock','supplier_return','scrap')):
            with self.subTest(kind=kind):
                source=self.source(kind,before=6000-index*1000);capture=self.capture(source);response,command=self.apply(capture)
                self.assertEqual(response.status_code,201,response.content);receipt=response.data['business_receipt']
                self.assertEqual((receipt['actor_id'],receipt['after_quantity_milli']),(str(source.origin_user_id),5000-index*1000))
                self.assertEqual(receipt['cost'],source.payload['decision']['cost'])
                self.assertEqual(self.client.post(self.url(f'operations/{capture.id}/integrate/'),command,format='json').data,response.data)
                self.assertEqual(self.preview(capture)['outcome'],'already_known');ack,_=self.apply(capture)
                self.assertEqual(ack.data['business_receipt'],receipt)
        self.assertEqual(remaining_unsellable(self.received),3000)
        self.assertEqual((NonSellableDecision.objects.count(),StockMovement.objects.count(),ReturnReceipt.objects.count()),(3,3,1))
        self.assertEqual(StockMovement.objects.get(kind='unsellable_restock').quantity_milli,1000)
        self.assertEqual(list(CashMovement.objects.values_list('amount_fcfa',flat=True)),cash);self.assertEqual(CustomerLedgerEntry.objects.count(),ledger)
        row=SupplierLedgerEntry.objects.get();self.assertEqual((row.delta_fcfa,row.debt_delta_fcfa,row.advance_delta_fcfa,row.credit_delta_fcfa),(0,0,0,0))
        self.assertFalse(SupplierDebt.objects.exists());self.assertFalse(SupplierCredit.objects.exists());self.assertFalse(SupplierReturn.objects.exists());self.assertFalse(StockLoss.objects.exists())

    def test_changed_but_sufficient_pool_keeps_source_facts_and_uses_current_remaining_ack(self):
        source=self.source();capture=self.capture(source);old=self.preview(capture)
        native=self.client.post(self.native_url(),self.native_body(self.source('scrap')),format='json');self.assertEqual(native.status_code,201,native.content)
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        first=response.data['business_receipt'];self.assertEqual(first['after_quantity_milli'],4000)
        self.assertEqual(Activity.objects.get(id=source.operation_id).details['recovery']['source_snapshot']['after_quantity_milli'],5000)
        native=self.client.post(self.native_url(),self.native_body(self.source('scrap',before=4000)),format='json');self.assertEqual(native.status_code,201,native.content)
        ack,_=self.apply(capture);self.assertEqual(ack.data['business_receipt'],first);self.assertEqual(remaining_unsellable(self.received),3000)

    def test_insufficient_pool_keeps_raw_copy_without_capping_quantity_or_adding_stock(self):
        source=self.source(quantity=2000);capture=self.capture(source)
        native=self.client.post(self.native_url(),self.native_body(self.source('scrap',5000)),format='json');self.assertEqual(native.status_code,201,native.content)
        preview=self.preview(capture);self.assertFalse(preview['can_apply']);self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        self.assertEqual(remaining_unsellable(self.received),1000);self.assertFalse(StockMovement.objects.filter(kind='unsellable_restock').exists())

    def test_missing_receipt_or_supplier_and_missing_dependencies_are_preserved_and_blocked(self):
        for case,code in (('receipt','missing_unsellable_receipt'),('supplier','missing_supplier')):
            with self.subTest(case=case):
                source=self.source('supplier_return' if case=='supplier' else 'restock')
                target='receipt_id' if case=='receipt' else 'supplier_id';identifier=str(uuid.uuid4())
                source.payload['decision'][target]=source.payload['original_request'][target]=identifier
                capture=self.capture(source,[uuid.uuid4()]);preview=self.preview(capture);self.assertFalse(preview['can_apply'])
                self.assertIn(code,{row['code'] for row in preview['issues']});self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        self.assertFalse(NonSellableDecision.objects.exists());self.assertEqual(remaining_unsellable(self.received),6000)

    def test_late_failure_rolls_back_pool_stock_supplier_ledger_origin_and_decision(self):
        for kind in ('restock','supplier_return','scrap'):
            with self.subTest(kind=kind):
                capture=self.capture(self.source(kind));preview=self.preview(capture)
                with connection.cursor() as cursor:cursor.execute("CREATE TEMP TRIGGER fail_pool_decision BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected disposition failure'); END")
                try:
                    with self.assertRaises(IntegrityError):self.apply(capture,preview)
                finally:
                    with connection.cursor() as cursor:cursor.execute('DROP TRIGGER fail_pool_decision')
                self.assertEqual(remaining_unsellable(self.received),6000);self.assertEqual(StockMovement.objects.count(),2)
                self.assertFalse(NonSellableDecision.objects.exists());self.assertFalse(SupplierLedgerEntry.objects.exists())
                self.assertFalse(backups.RecoveredOperationOrigin.objects.exists())

    def test_unknown_and_known_zero_costs_are_not_confused(self):
        from django.db import transaction
        initial=(self.sale,self.returned,self.parent_line,self.origin_cost,self.receipt_data,self.received)
        for micros,state,total in ((None,'unknown',None),(0,'known','0')):
            with self.subTest(state=state),transaction.atomic():
                StockMovement.objects.filter(kind='opening').update(unit_cost_micros=micros)
                self.prepare_pool(self.prepare_sale());source=self.source()
                response,_=self.apply(self.capture(source));self.assertEqual(response.status_code,201,response.content)
                receipt=response.data['business_receipt'];self.assertEqual((receipt['cost']['cost_state'],receipt['cost']['total_cost_nanos_fcfa']),(state,total))
                self.assertEqual(receipt['unit_cost_micros'],micros);self.assertEqual(receipt['cost_is_provisional'],state!='known')
                transaction.set_rollback(True)
            self.sale,self.returned,self.parent_line,self.origin_cost,self.receipt_data,self.received=initial

    def prepare_fractional_pool(self):
        self.product.unit_kind='weight';self.product.unit_label='kg';self.product.save()
        sale=self.prepare_sale(lines=[{'id':str(uuid.uuid4()),'product_id':str(self.product.id),
            'product_version':self.product.version,'quantity_milli':3}],payments=[{'id':str(uuid.uuid4()),'method':'cash','amount_fcfa':2}])
        self.prepare_return(sale)
        self.origin_cost.total_cost_nanos_fcfa=1000000000;self.origin_cost.unit_cost_micros_numerator=1000000000
        self.origin_cost.unit_cost_micros_denominator=3;self.origin_cost.save()
        source=fixtures.ReceiptSources.source(self,'unsellable',3)
        body={key:value for key,value in source.payload['original_request'].items() if key not in ('return_line_id','installation_id')}
        response=self.client.post(self.receipt_url(),body,format='json');self.assertEqual(response.status_code,201,response.content)
        self.receipt_data=response.data;self.received=ReturnReceipt.objects.get(id=response.data['id'])

    def test_exact_fractional_cost_is_conserved_across_restock_scrap_and_restock(self):
        self.prepare_fractional_pool();totals=[]
        for kind,before,total in (('restock',3,'333333333'),('scrap',2,'333333334'),('restock',1,'333333333')):
            with self.subTest(kind=kind,before=before):
                response,_=self.apply(self.capture(self.source(kind,1,before=before)));self.assertEqual(response.status_code,201,response.content)
                receipt=response.data['business_receipt'];self.assertEqual(receipt['cost']['total_cost_nanos_fcfa'],total)
                self.assertIsNone(receipt['unit_cost_micros']);self.assertFalse(receipt['cost_is_provisional']);totals.append(int(total))
        self.assertEqual((sum(totals),remaining_unsellable(self.received)),(1000000000,0))

    def test_different_cumulative_rounding_blocks_full_source_without_changing_its_quantity(self):
        self.prepare_fractional_pool();source=self.source('restock',1,before=3);capture=self.capture(source)
        response=self.client.post(self.native_url(),self.native_body(self.source('scrap',1,before=3)),format='json');self.assertEqual(response.status_code,201,response.content)
        preview=self.preview(capture);self.assertFalse(preview['can_apply'])
        self.assertIn('decision_cost_context_changed',{row['code'] for row in preview['issues']})
        self.assertEqual(self.apply(capture,preview)[0].status_code,409);self.assertEqual(remaining_unsellable(self.received),2)
        self.assertEqual(preview['snapshot']['quantity_milli'],1)

    def revise_pool_cost(self):
        from stock.models import CostRevaluation,StockCostSnapshot,ReturnCostRevision
        operation=Activity.objects.create(shop=self.a,actor=self.owner,action='cost_revaluation',target_id=self.product.id,reason='Révision de coût de fixture')
        batch=CostRevaluation.objects.create(product=self.product,operation=operation,installation=self.live.installation)
        sale_cost=StockCostSnapshot.objects.create(movement=self.origin_cost.movement,trigger_operation=operation,version=2,supersedes=self.origin_cost,
            cost_state='known',total_cost_nanos_fcfa=2000000000000,unit_cost_micros_numerator=200000000,unit_cost_micros_denominator=1,
            quantity_after_milli=self.origin_cost.quantity_after_milli,stock_value_after_nanos_fcfa=self.origin_cost.stock_value_after_nanos_fcfa,
            valuation_requires_regularization=self.origin_cost.valuation_requires_regularization,valuation_gap_nanos_fcfa=self.origin_cost.valuation_gap_nanos_fcfa)
        return ReturnCostRevision.objects.create(origin=self.received.cost_snapshot,batch=batch,version=2,supersedes=None,sale_cost=sale_cost,cost_state='known',
            total_cost_nanos_fcfa=1200000000000,unit_cost_micros_numerator=200000000,unit_cost_micros_denominator=1)

    def test_historical_cost_version_is_preserved_and_current_revision_remains_visible_between_generations(self):
        source=self.source();capture=self.capture(source);old=self.preview(capture);revision=self.revise_pool_cost()
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        preview=self.preview(capture);self.assertTrue(preview['can_apply'])
        self.assertIn('newer_receipt_cost_kept_distinct',{row['code'] for row in preview['notices']})
        response,_=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)
        first=response.data['business_receipt'];self.assertEqual(first['cost']['version'],1);self.assertEqual(first['unit_cost_micros'],100000000)
        current=self.client.get(self.native_url()+first['id']+'/');self.assertEqual(current.status_code,200,current.content)
        self.assertEqual((current.data['cost']['version'],current.data['unit_cost_micros']),(2,200000000))
        self.assertEqual(current.data['cost']['revision_id'],str(revision.id))
        self.current.current_slot=None;self.current.save();self.current=self.generation();self.registry=self.record(self.current)
        self.review(self.a,self.owner,'owner',confirmed=True)
        capture=self.capture(source);response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.data['business_receipt'],first);self.assertEqual(remaining_unsellable(self.received),5000)
        self.assertEqual(StockMovement.objects.get(kind='unsellable_restock').unit_cost_micros,100000000)

    def test_native_decision_with_a_recorded_revision_is_known_after_later_disposition(self):
        from stock.api.return_cost_serializers import cost_representation
        revision=self.revise_pool_cost();self.receipt_data['cost']=cost_representation(revision)
        self.receipt_data['unit_cost_micros']=200000000
        source=self.source();source.origin_user_id=self.owner.id;source.origin_installation_id=self.live.installation_id
        source.payload['original_request']['installation_id']=str(self.live.installation_id)
        native=self.client.post(self.native_url(),self.native_body(source),format='json');self.assertEqual(native.status_code,201,native.content)
        original=native.data
        for key in source.payload['decision']:
            if key in original:source.payload['decision'][key]=original[key]
        native=self.client.post(self.native_url(),self.native_body(self.source('scrap',before=5000)),format='json');self.assertEqual(native.status_code,201,native.content)
        capture=self.capture(source);preview=self.preview(capture);self.assertEqual(preview['outcome'],'already_known')
        response,_=self.apply(capture,preview);self.assertEqual(response.data['business_receipt'],original)
        self.assertEqual(remaining_unsellable(self.received),4000);self.assertFalse(backups.RecoveredOperationOrigin.objects.exists())

    def test_native_original_request_boolean_spelling_is_recognized_after_canonicalization(self):
        source=self.source();source.origin_user_id=self.owner.id;source.origin_installation_id=self.live.installation_id
        source.payload['original_request'].update(installation_id=str(self.live.installation_id),is_sellable_confirmed='true')
        native=self.client.post(self.native_url(),self.native_body(source),format='json');self.assertEqual(native.status_code,201,native.content)
        for key in source.payload['decision']:
            if key in native.data:source.payload['decision'][key]=native.data[key]
        capture=self.capture(source);preview=self.preview(capture);self.assertEqual(preview['outcome'],'already_known')
        response,_=self.apply(capture,preview);self.assertEqual(response.data['business_receipt'],native.data)
        self.assertEqual((NonSellableDecision.objects.count(),remaining_unsellable(self.received)),(1,5000))

    def test_current_rights_native_bypass_and_foreign_sources_do_not_integrate(self):
        from identity.models import User,Membership
        source=self.source();capture=self.capture(source)
        seller=User.objects.create_user(username='disposition-seller');membership=Membership.objects.create(shop=self.a,user=seller)
        self.review(self.a,seller,'seller',membership=membership,confirmed=True);api=self.authenticate(seller)
        self.assertEqual(api.generic('POST',self.url(f'operations/{capture.id}/integrate/'),b'invalid',content_type='application/json').status_code,403)
        self.assertEqual(self.client.post(self.native_url(),self.native_body(source),format='json').status_code,409)
        self.assertEqual(self.client.get(self.url(f'operations/{capture.id}/integration-preview/',shop=self.b)).status_code,404)
        old=self.preview(capture);review=backups.RestoreAccessReview.objects.get(generation=self.current,shop=self.a,user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=1,version=2,state='refused',reason='Qualité actuelle retirée')
        self.assertEqual(self.apply(capture,old)[0].status_code,404);self.assertFalse(NonSellableDecision.objects.exists())

    def test_foreign_receipt_and_supplier_do_not_reveal_their_current_state(self):
        from sales.models import Sale
        source=self.source();Sale.objects.filter(id=self.sale['id']).update(shop=self.b)
        preview=self.preview(self.capture(source));self.assertFalse(preview['can_apply']);self.assertIsNone(preview['current_state']);self.assertEqual(preview['stocks'],[])
        Sale.objects.filter(id=self.sale['id']).update(shop=self.a)
        Supplier.objects.filter(id=self.supplier.id).update(shop=self.b)
        preview=self.preview(self.capture(self.source('supplier_return')));self.assertFalse(preview['can_apply'])
        self.assertIsNone(preview['current_state']['supplier_id']);self.assertIn('missing_supplier',{row['code'] for row in preview['issues']})

    def test_corrupt_origin_refuses_list_and_detail_without_new_effect(self):
        source=self.source();capture=self.capture(source);response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        backups.RecoveryJournalOperation.objects.filter(id=capture.id).update(shop=self.b,origin_user_id=uuid.uuid4())
        for language in ('fr','en'):
            for suffix in ('',source.payload['decision']['id']+'/'):
                with self.subTest(language=language,suffix=suffix):
                    response=self.client.get(self.native_url()+suffix,HTTP_ACCEPT_LANGUAGE=language)
                    self.assertEqual((response.status_code,response.data['code']),(409,'origin_reference_conflict'))
        self.assertEqual((remaining_unsellable(self.received),NonSellableDecision.objects.count(),StockMovement.objects.count()),(5000,1,3))

    def test_legacy_cost_absence_remains_explicit_after_later_reconstruction(self):
        from stock.models import ReturnCostSnapshot,ReturnCostReconstruction,CostRevaluation
        ReturnCostSnapshot.objects.filter(receipt=self.received).delete()
        operation=self.received.operation;operation.details['response']['cost']=None;operation.save(update_fields=['details'])
        self.receipt_data['cost']=None;source=self.source();capture=self.capture(source)
        event=Activity.objects.create(shop=self.a,actor=self.owner,action='cost_revaluation',target_id=self.product.id,reason='Reconstitution historique de fixture')
        batch=CostRevaluation.objects.create(product=self.product,operation=event,installation=self.live.installation)
        base=ReturnCostSnapshot.objects.create(receipt=self.received,sale_cost=None,cost_state='known',total_cost_nanos_fcfa=600000000000,
            unit_cost_micros_numerator=100000000,unit_cost_micros_denominator=1)
        ReturnCostReconstruction.objects.create(original=base,batch=batch,previous_legacy_unit_cost_micros=100000000,previous_cost_is_provisional=False)
        response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        first=response.data['business_receipt'];self.assertIsNone(first['cost']);self.assertEqual(first['unit_cost_micros'],100000000)
        current=self.client.get(self.native_url()+first['id']+'/');self.assertEqual(current.status_code,200,current.content)
        self.assertEqual(current.data['cost']['origin_kind'],'reconstructed_legacy')
        ack,_=self.apply(capture);self.assertEqual(ack.data['business_receipt'],first);self.assertEqual(remaining_unsellable(self.received),5000)

    def test_missing_cost_version_keeps_source_without_using_current_cost(self):
        source=self.source();source.payload['decision']['cost']['receipt_cost_id']=str(uuid.uuid4())
        capture=self.capture(source);preview=self.preview(capture);self.assertFalse(preview['can_apply'])
        self.assertIn('missing_receipt_cost_version',{row['code'] for row in preview['issues']})
        self.assertEqual(self.apply(capture,preview)[0].status_code,409);self.assertEqual(remaining_unsellable(self.received),6000)

    def test_live_previews_receipts_history_and_bilingual_errors_match_openapi(self):
        import json
        from drf_spectacular.generators import SchemaGenerator
        from jsonschema import Draft4Validator,FormatChecker
        def nullable(value):
            if isinstance(value,list):return [nullable(item) for item in value]
            if not isinstance(value,dict):return value
            result={key:nullable(item) for key,item in value.items() if key!='nullable'}
            return {'anyOf':[result,{'type':'null'}]} if value.get('nullable') else result
        schema=SchemaGenerator().get_schema(request=None,public=True)
        def check(response,ending,method='get'):
            path=next(path for path in schema['paths'] if path.endswith(ending))
            rule=schema['paths'][path][method]['responses'][str(response.status_code)]['content']['application/json']['schema']
            errors=list(Draft4Validator(nullable({**rule,'components':schema['components']}),format_checker=FormatChecker()).iter_errors(json.loads(response.content)))
            self.assertEqual([(list(error.absolute_path),error.message) for error in errors],[])
        for index,kind in enumerate(('restock','supplier_return','scrap')):
            with self.subTest(kind=kind):
                source=self.source(kind,before=6000-index*1000);capture=self.capture(source)
                preview=self.client.get(self.url(f'operations/{capture.id}/integration-preview/'));self.assertEqual(preview.status_code,200,preview.content)
                check(preview,'/integration-preview/')
                response,_=self.apply(capture,preview.data);self.assertEqual(response.status_code,201,response.content);check(response,'/integrate/','post')
                history=self.client.get(self.native_url());self.assertEqual(history.status_code,200,history.content)
                check(history,'/receipts/{receipt_id}/decisions/')
                self.assertEqual(next(row for row in history.data['results'] if row['id']==source.payload['decision']['id'])['actor_id'],str(source.origin_user_id))
        source=self.source();source.payload['decision']['is_sellable_confirmed']=False;capture=self.capture(source)
        for language,detail in (('en','Recovery non-sellable disposition snapshot is inconsistent.'),
                ('fr','Les données figées de la décision sur retour non vendable récupérée sont incohérentes.')):
            with self.subTest(language=language):
                response=self.client.get(self.url(f'operations/{capture.id}/integration-preview/'),HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual((response.status_code,str(response.data['detail'])),(400,detail))
