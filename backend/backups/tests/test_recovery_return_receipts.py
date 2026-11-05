"""Recovered physical returns preserve condition, exact original costs and one stock effect."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import uuid
from django.db import connection,IntegrityError
from django.test import TestCase,SimpleTestCase
from backups import models as backups
from backups.tests import test_recovery_sale_returns as fixtures
from common.models import Activity
from cash.models import CashMovement
from customers.models import CustomerLedgerEntry
from sales.models import SaleReturnLine
from stock.models import ReturnReceipt,ReturnCostSnapshot,StockCostSnapshot,StockMovement
from stock.services.moving_average_cost import returned_cost_nanos
from stock.services.return_costs import integral_unit_cost


class ReceiptSources:
    def source(self,condition='sellable',quantity=2000,*,before=0):
        operation,actor,device,received,cost_id=[uuid.uuid4() for _ in range(5)]
        occurred=datetime(2026,10,24,12,tzinfo=timezone.utc);origin=self.origin_cost
        values={'cost_state':origin.cost_state,'total_cost_nanos_fcfa':returned_cost_nanos(origin.total_cost_nanos_fcfa,
            self.parent_line.original_quantity_milli,before,quantity),'unit_cost_micros_numerator':origin.unit_cost_micros_numerator,
            'unit_cost_micros_denominator':origin.unit_cost_micros_denominator}
        cost={'receipt_cost_id':str(cost_id),'revision_id':None,'version':1,'origin_kind':'frozen','sale_cost_id':str(origin.id),
            'cost_state':values['cost_state'],**{key:None if value is None else str(value) for key,value in values.items() if key!='cost_state'}}
        raw={'id':str(received),'operation_id':str(operation),'return_line_id':str(self.parent_line.id),
            'installation_id':str(device),'occurred_at':occurred.isoformat(),'reason':'Marchandise physiquement vérifiée',
            'quantity_milli':quantity,'condition':condition}
        snapshot={'id':str(received),'sale_id':self.sale['id'],'sale_return_id':self.returned['id'],
            'return_line_id':str(self.parent_line.id),'product_id':str(self.product.id),'unit_kind':self.parent_line.unit_kind,
            'quantity_milli':quantity,'condition':condition,'occurred_at':occurred.isoformat(),
            'created_at':(occurred+timedelta(seconds=5)).isoformat(),
            'movement_id':str(uuid.uuid5(received,'return-stock')) if condition=='sellable' else None,
            'unit_cost_micros':integral_unit_cost(values),'cost_is_provisional':values['cost_state']!='known','cost':cost}
        return SimpleNamespace(operation_type='return_receipt',operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=occurred,payload={'schema_version':1,'original_request':raw,'receipt':snapshot})


class RecoveryReceiptSnapshotTests(SimpleTestCase,ReceiptSources):
    def setUp(self):
        self.sale={'id':str(uuid.uuid4())};self.returned={'id':str(uuid.uuid4())};self.product=SimpleNamespace(id=uuid.uuid4())
        self.parent_line=SimpleNamespace(id=uuid.uuid4(),original_quantity_milli=10000,unit_kind='piece')
        self.origin_cost=SimpleNamespace(id=uuid.uuid4(),cost_state='known',total_cost_nanos_fcfa=1000000000000,
            unit_cost_micros_numerator=100000000,unit_cost_micros_denominator=1)

    def test_source_identity_physical_effect_exact_cost_and_legacy_projection_are_checked(self):
        from backups.services.recovery_return_receipts import decode_return_receipt,RecoveryReturnReceiptInvalid
        self.assertEqual(decode_return_receipt(self.source()).snapshot['quantity_milli'],2000)
        for case in ('quantity','piece','condition','movement','projection','state','denominator','integer','version','revision','time','origin','stock'):
            with self.subTest(case=case):
                source=self.source();snapshot=source.payload['receipt'];raw=source.payload['original_request']
                if case=='quantity':snapshot['quantity_milli']=3000
                elif case=='piece':snapshot['quantity_milli']=raw['quantity_milli']=1500
                elif case=='condition':snapshot['condition']='unsellable'
                elif case=='movement':snapshot['movement_id']=str(uuid.uuid4())
                elif case=='projection':snapshot['unit_cost_micros']=1
                elif case=='state':snapshot['cost']['cost_state']='unknown'
                elif case=='denominator':snapshot['cost']['unit_cost_micros_denominator']='0'
                elif case=='integer':snapshot['cost']['total_cost_nanos_fcfa']=True
                elif case=='version':snapshot['cost']['version']=2
                elif case=='revision':snapshot['cost']['revision_id']=str(uuid.uuid4())
                elif case=='time':snapshot['occurred_at']='2026-10-24T13:00:00Z'
                elif case=='origin':raw['installation_id']=str(uuid.uuid4())
                else:snapshot['stock_after_milli']=5000
                with self.assertRaises(RecoveryReturnReceiptInvalid):decode_return_receipt(source)


class RecoveryReturnReceiptTests(TestCase,ReceiptSources):
    authenticate=fixtures.RecoverySaleReturnTests.authenticate
    generation=fixtures.RecoverySaleReturnTests.generation
    record=fixtures.RecoverySaleReturnTests.record
    device=fixtures.RecoverySaleReturnTests.device
    review=fixtures.RecoverySaleReturnTests.review
    payload=fixtures.RecoverySaleReturnTests.payload
    url=fixtures.RecoverySaleReturnTests.url
    capture=fixtures.RecoverySaleReturnTests.capture
    preview=fixtures.RecoverySaleReturnTests.preview
    apply=fixtures.RecoverySaleReturnTests.apply
    prepare_sale=fixtures.RecoverySaleReturnTests.prepare_sale

    def setUp(self):
        fixtures.RecoverySaleReturnTests.setUp(self)
        self.prepare_return(self.sale)

    def prepare_return(self,sale):
        self.sale=sale;source=fixtures.ReturnSources.source(self,sale)
        body={key:value for key,value in source.payload['original_request'].items() if key not in ('sale_id','installation_id')}
        response=self.client.post(f"/api/v1/shops/{self.a.id}/sales/{sale['id']}/returns/",body,format='json')
        self.assertEqual(response.status_code,201,response.content);self.returned=response.data
        self.return_source=source
        self.parent_line=SaleReturnLine.objects.get(id=self.returned['lines'][0]['id'])
        self.origin_cost=StockCostSnapshot.objects.filter(movement__sale_line_id=self.parent_line.sale_line_id).order_by('-version').first()

    def native_url(self):
        return f"/api/v1/shops/{self.a.id}/sales/{self.sale['id']}/returns/{self.returned['id']}/lines/{self.parent_line.id}/receipts/"

    def native_body(self,source):
        return {key:value for key,value in source.payload['original_request'].items() if key not in ('return_line_id','installation_id')}

    def test_sellable_and_unsellable_partial_receipts_preserve_costs_and_only_sellable_stock(self):
        money=list(CashMovement.objects.values_list('amount_fcfa',flat=True));ledger=CustomerLedgerEntry.objects.count()
        self.product.is_active=False;self.product.purchase_price_fcfa=9999;self.product.save()
        for condition,quantity in (('unsellable',2000),('sellable',3000)):
            with self.subTest(condition=condition):
                source=self.source(condition,quantity,before=0 if condition=='unsellable' else 2000)
                capture=self.capture(source);response,command=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
                receipt=response.data['business_receipt'];self.assertEqual(receipt['actor_id'],str(source.origin_user_id))
                self.assertEqual(receipt['cost'],source.payload['receipt']['cost'])
                self.assertEqual(receipt['remaining_unsellable_milli'],quantity if condition=='unsellable' else 0)
                retry=self.client.post(self.url(f'operations/{capture.id}/integrate/'),command,format='json')
                self.assertEqual((retry.status_code,retry.data),(200,response.data))
                self.assertEqual(self.preview(capture)['outcome'],'already_known')
                ack,_=self.apply(capture);self.assertEqual(ack.data['business_receipt'],receipt)
                history=self.client.get(self.native_url());self.assertEqual(history.status_code,200,history.content)
                self.assertEqual(next(row for row in history.data['results'] if row['id']==receipt['id'])['actor_id'],str(source.origin_user_id))
        self.assertEqual(list(CashMovement.objects.values_list('amount_fcfa',flat=True)),money)
        self.assertEqual(CustomerLedgerEntry.objects.count(),ledger)
        self.assertEqual((ReturnReceipt.objects.count(),ReturnCostSnapshot.objects.count()),(2,2))
        self.assertEqual(list(StockMovement.objects.filter(kind='customer_return').values_list('quantity_milli',flat=True)),[3000])

    def test_changed_physical_quantity_invalidates_preview_and_blocks_over_receipt_without_partial_stock(self):
        source=self.source();capture=self.capture(source);old=self.preview(capture)
        native=self.client.post(self.native_url(),self.native_body(self.source('unsellable',9000)),format='json')
        self.assertEqual(native.status_code,201,native.content)
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        fresh=self.preview(capture);self.assertFalse(fresh['can_apply']);self.assertEqual(self.apply(capture,fresh)[0].status_code,409)
        self.assertEqual(ReturnReceipt.objects.count(),1);self.assertFalse(StockMovement.objects.filter(kind='customer_return').exists())

    def test_missing_parent_and_missing_sale_cost_remain_raw_without_guessing_cost_or_stock(self):
        for case,code in (('parent','missing_return_line'),('cost','missing_sale_cost')):
            with self.subTest(case=case):
                source=self.source()
                if case=='parent':
                    identifier=str(uuid.uuid4());source.payload['receipt']['return_line_id']=identifier;source.payload['original_request']['return_line_id']=identifier
                else:source.payload['receipt']['cost']['sale_cost_id']=str(uuid.uuid4())
                capture=self.capture(source);preview=self.preview(capture);self.assertFalse(preview['can_apply'])
                self.assertIn(code,{row['code'] for row in preview['issues']});self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        self.assertFalse(ReturnReceipt.objects.exists());self.assertFalse(StockMovement.objects.filter(kind='customer_return').exists())

    def test_late_decision_failure_rolls_back_physical_pool_stock_cost_origin_and_alerts(self):
        capture=self.capture(self.source());preview=self.preview(capture);stock=StockMovement.objects.count()
        with connection.cursor() as cursor:cursor.execute("CREATE TEMP TRIGGER fail_physical_return BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected physical receipt failure'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(capture,preview)
        finally:
            with connection.cursor() as cursor:cursor.execute('DROP TRIGGER fail_physical_return')
        self.assertEqual((ReturnReceipt.objects.count(),ReturnCostSnapshot.objects.count(),StockMovement.objects.count()),(0,0,stock))
        self.assertFalse(backups.RecoveredOperationOrigin.objects.exists())

    def test_unknown_cost_never_becomes_zero_after_recovery(self):
        StockMovement.objects.filter(kind='opening').update(unit_cost_micros=None)
        self.prepare_return(self.prepare_sale());source=self.source()
        self.assertEqual(source.payload['receipt']['cost']['cost_state'],'unknown')
        response,_=self.apply(self.capture(source));self.assertEqual(response.status_code,201,response.content)
        receipt=response.data['business_receipt'];self.assertIsNone(receipt['cost']['total_cost_nanos_fcfa'])
        self.assertIsNone(receipt['unit_cost_micros']);self.assertTrue(receipt['cost_is_provisional'])

    def test_known_zero_cost_remains_known_zero(self):
        StockMovement.objects.filter(kind='opening').update(unit_cost_micros=0)
        self.prepare_return(self.prepare_sale());source=self.source()
        response,_=self.apply(self.capture(source));self.assertEqual(response.status_code,201,response.content)
        receipt=response.data['business_receipt'];self.assertEqual(receipt['cost']['total_cost_nanos_fcfa'],'0')
        self.assertEqual(receipt['unit_cost_micros'],0);self.assertFalse(receipt['cost_is_provisional'])

    def test_preview_exact_costs_are_decimal_strings_even_beyond_binary64(self):
        numerator=(1<<75)+1
        self.origin_cost.unit_cost_micros_numerator=numerator;self.origin_cost.unit_cost_micros_denominator=3
        self.origin_cost.total_cost_nanos_fcfa=(numerator*self.parent_line.original_quantity_milli+1)//3;self.origin_cost.save()
        source=self.source();preview=self.preview(self.capture(source));self.assertTrue(preview['can_apply'])
        expected=preview['current_state']['expected_cost']
        for key in ('total_cost_nanos_fcfa','unit_cost_micros_numerator','unit_cost_micros_denominator'):
            self.assertIs(type(expected[key]),str);self.assertEqual(expected[key],source.payload['receipt']['cost'][key])

    def test_fractional_cost_shares_preserve_333333333_333333334_333333333(self):
        self.product.unit_kind='weight';self.product.unit_label='kg';self.product.save()
        sale=self.prepare_sale(lines=[{'id':str(uuid.uuid4()),'product_id':str(self.product.id),
            'product_version':self.product.version,'quantity_milli':3}],payments=[{'id':str(uuid.uuid4()),'method':'cash','amount_fcfa':2}])
        self.prepare_return(sale)
        # A retained exact sale-cost fixture, independent from current catalogue cost.
        self.origin_cost.total_cost_nanos_fcfa=1000000000;self.origin_cost.unit_cost_micros_numerator=1000000000
        self.origin_cost.unit_cost_micros_denominator=3;self.origin_cost.save()
        for before,total in enumerate((333333333,333333334,333333333)):
            with self.subTest(before=before):
                source=self.source('unsellable',1,before=before);capture=self.capture(source)
                response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
                self.assertEqual(response.data['business_receipt']['cost']['total_cost_nanos_fcfa'],str(total))
                self.assertIsNone(response.data['business_receipt']['unit_cost_micros'])
        self.assertEqual(sum(ReturnCostSnapshot.objects.values_list('total_cost_nanos_fcfa',flat=True)),1000000000)
        self.assertFalse(StockMovement.objects.filter(kind='customer_return').exists())

    def test_native_receipt_is_known_after_scrap_without_resetting_unsellable_quantity(self):
        from stock.services.unsellable_balance import remaining_unsellable
        source=self.source('unsellable');source.origin_user_id=self.owner.id;source.origin_installation_id=self.live.installation_id
        source.payload['original_request']['installation_id']=str(self.live.installation_id)
        response=self.client.post(self.native_url(),self.native_body(source),format='json');self.assertEqual(response.status_code,201,response.content)
        original=response.data
        for key in source.payload['receipt']:
            if key in original:source.payload['receipt'][key]=original[key]
        decision=self.client.post(self.native_url()+original['id']+'/decisions/',{'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),
            'occurred_at':'2026-10-25T12:00:00Z','reason':'Rebut réel ultérieur','kind':'scrap','quantity_milli':500},format='json')
        self.assertEqual(decision.status_code,400,decision.content)
        decision=self.client.post(self.native_url()+original['id']+'/decisions/',{'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),
            'occurred_at':'2026-10-25T12:00:00Z','reason':'Rebut réel ultérieur','kind':'scrap','quantity_milli':1000},format='json')
        self.assertEqual(decision.status_code,201,decision.content)
        capture=self.capture(source);preview=self.preview(capture);self.assertEqual(preview['outcome'],'already_known')
        response,_=self.apply(capture,preview);self.assertEqual((response.status_code,response.data['business_receipt']),(201,original))
        self.assertEqual(remaining_unsellable(ReturnReceipt.objects.get()),1000)
        self.assertFalse(backups.RecoveredOperationOrigin.objects.exists())

    def test_legacy_receipt_survives_later_explicit_cost_reconstruction(self):
        from stock.models import CostRevaluation,ReturnCostReconstruction
        # Older sales stored this integer directly, before exact cost snapshots.
        self.parent_line.sale_line.unit_cost_micros=100000000;self.parent_line.sale_line.save(update_fields=['unit_cost_micros'])
        source=self.source('unsellable');source.payload['receipt']['cost']=None;capture=self.capture(source)
        response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content);original=response.data['business_receipt']
        received=ReturnReceipt.objects.get();self.assertIsNone(original['cost']);self.assertFalse(ReturnCostSnapshot.objects.exists())
        operation=Activity.objects.create(shop=self.a,actor=self.owner,action='cost_revaluation',target_id=self.product.id,reason='Reconstitution explicite de fixture')
        batch=CostRevaluation.objects.create(product=self.product,operation=operation,installation=self.live.installation)
        baseline=ReturnCostSnapshot.objects.create(receipt=received,sale_cost=None,cost_state='known',total_cost_nanos_fcfa=200000000000,
            unit_cost_micros_numerator=100000000,unit_cost_micros_denominator=1)
        ReturnCostReconstruction.objects.create(original=baseline,batch=batch,previous_legacy_unit_cost_micros=100000000,previous_cost_is_provisional=False)
        preview=self.preview(capture);self.assertEqual(preview['outcome'],'already_known')
        response,_=self.apply(capture,preview);self.assertEqual(response.data['business_receipt'],original)

    def test_provisional_original_cost_remains_provisional(self):
        StockMovement.objects.filter(kind='opening').update(quantity_milli=5000)
        self.prepare_return(self.prepare_sale());source=self.source()
        self.assertEqual(source.payload['receipt']['cost']['cost_state'],'provisional')
        response,_=self.apply(self.capture(source));self.assertEqual(response.status_code,201,response.content)
        self.assertTrue(response.data['business_receipt']['cost_is_provisional'])
        self.assertEqual(response.data['business_receipt']['cost']['cost_state'],'provisional')

    def test_original_receipt_after_cost_revision_and_new_generation_keeps_first_acknowledgement(self):
        from stock.models import CostRevaluation,ReturnCostRevision
        source=self.source();capture=self.capture(source);response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        original=response.data['business_receipt'];received=ReturnReceipt.objects.get();baseline=received.cost_snapshot
        operation=Activity.objects.create(shop=self.a,actor=self.owner,action='cost_revaluation',target_id=self.product.id,reason='Révision explicite de fixture')
        batch=CostRevaluation.objects.create(product=self.product,operation=operation,installation=self.live.installation)
        revised=StockCostSnapshot.objects.create(movement=self.origin_cost.movement,trigger_operation=operation,version=2,supersedes=self.origin_cost,
            cost_state='known',total_cost_nanos_fcfa=2000000000000,unit_cost_micros_numerator=200000000,unit_cost_micros_denominator=1,
            quantity_after_milli=self.origin_cost.quantity_after_milli,stock_value_after_nanos_fcfa=self.origin_cost.stock_value_after_nanos_fcfa,
            valuation_requires_regularization=self.origin_cost.valuation_requires_regularization,valuation_gap_nanos_fcfa=self.origin_cost.valuation_gap_nanos_fcfa)
        ReturnCostRevision.objects.create(origin=baseline,batch=batch,version=2,supersedes=None,sale_cost=revised,cost_state='known',
            total_cost_nanos_fcfa=400000000000,unit_cost_micros_numerator=200000000,unit_cost_micros_denominator=1)
        self.current.current_slot=None;self.current.save();self.current=self.generation();self.registry=self.record(self.current)
        self.review(self.a,self.owner,'owner',confirmed=True)
        capture=self.capture(source);preview=self.preview(capture);self.assertEqual(preview['outcome'],'already_known')
        response,_=self.apply(capture,preview);self.assertEqual(response.data['business_receipt'],original)
        self.assertNotEqual(datetime.fromisoformat(original['created_at'].replace('Z','+00:00')),
            datetime.fromisoformat(source.payload['receipt']['created_at']))
        activity=Activity.objects.get(id=source.operation_id)
        self.assertEqual(datetime.fromisoformat(activity.details['recovery']['source_snapshot']['created_at'].replace('Z','+00:00')),
            datetime.fromisoformat(source.payload['receipt']['created_at']))
        history=self.client.get(self.native_url()+str(received.id)+'/');self.assertEqual(history.status_code,200,history.content)
        self.assertEqual((history.data['unit_cost_micros'],history.data['cost']['version']),(200000000,2))
        self.assertEqual((ReturnReceipt.objects.count(),StockMovement.objects.filter(kind='customer_return').count()),(1,1))

    def test_missing_commercial_return_is_recovered_before_physical_receipt(self):
        from sales.models import SaleReturn
        physical=self.source();parent=deepcopy(self.return_source)
        parent.origin_user_id=self.owner.id;parent.origin_installation_id=self.live.installation_id
        parent.payload['original_request']['installation_id']=str(self.live.installation_id)
        for key in parent.payload['sale_return']:
            if key in self.returned:parent.payload['sale_return'][key]=self.returned[key]
        # Simulate loss of this T1 commercial return, leaving the T0 sale/cost.
        self.parent_line.delete();SaleReturn.objects.filter(id=self.returned['id']).delete()
        CustomerLedgerEntry.objects.filter(operation_id=parent.operation_id).delete();Activity.objects.filter(id=parent.operation_id).delete()
        capture=self.capture(physical,[parent.operation_id]);old=self.preview(capture);self.assertFalse(old['can_apply'])
        response,_=self.apply(self.capture(parent));self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual((SaleReturn.objects.count(),ReturnReceipt.objects.count(),StockMovement.objects.count()),(1,1,3))
        self.assertEqual(list(CashMovement.objects.values_list('amount_fcfa',flat=True)),[5000])
        self.assertEqual(ReturnCostSnapshot.objects.get().sale_cost_id,self.origin_cost.id)

    def test_current_rights_and_native_bypass_are_checked_before_integrating(self):
        from identity.models import User,Membership
        source=self.source();capture=self.capture(source)
        seller=User.objects.create_user(username='physical-receipt-seller');membership=Membership.objects.create(shop=self.a,user=seller)
        self.review(self.a,seller,'seller',membership=membership,confirmed=True);api=self.authenticate(seller)
        self.assertEqual(api.generic('POST',self.url(f'operations/{capture.id}/integrate/'),b'invalid',content_type='application/json').status_code,403)
        self.assertEqual(self.client.post(self.native_url(),self.native_body(source),format='json').status_code,409)
        self.assertEqual(self.client.get(self.url(f'operations/{capture.id}/integration-preview/',shop=self.b)).status_code,404)
        old=self.preview(capture);review=backups.RestoreAccessReview.objects.get(generation=self.current,shop=self.a,user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=1,version=2,state='refused',reason='Qualité actuelle retirée')
        self.assertEqual(self.apply(capture,old)[0].status_code,404);self.assertFalse(ReturnReceipt.objects.exists())

    def test_actual_foreign_return_or_cost_is_not_loaded_as_recovery_evidence(self):
        from sales.models import Sale
        source=self.source();Sale.objects.filter(id=self.sale['id']).update(shop=self.b)
        preview=self.preview(self.capture(source));self.assertFalse(preview['can_apply']);self.assertIsNone(preview['current_state'])
        self.assertEqual(preview['stocks'],[]);self.assertIn('missing_return_line',{row['code'] for row in preview['issues']})
        Sale.objects.filter(id=self.sale['id']).update(shop=self.a)
        foreign=self.prepare_sale();Sale.objects.filter(id=foreign['id']).update(shop=self.b)
        foreign_cost=StockCostSnapshot.objects.get(movement__sale_line__sale_id=foreign['id'])
        source=self.source();source.payload['receipt']['cost']['sale_cost_id']=str(foreign_cost.id)
        preview=self.preview(self.capture(source));self.assertFalse(preview['can_apply'])
        self.assertIn('missing_sale_cost',{row['code'] for row in preview['issues']})
        self.assertIsNone(preview['current_state']['sale_cost_id'])

    def test_corrupt_origin_refuses_native_list_and_detail_without_reassigning_physical_stock(self):
        source=self.source();capture=self.capture(source);response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        backups.RecoveryJournalOperation.objects.filter(id=capture.id).update(shop=self.b,origin_user_id=uuid.uuid4())
        for language in ('fr','en'):
            for suffix in ('',source.payload['receipt']['id']+'/'):
                with self.subTest(language=language,suffix=suffix):
                    response=self.client.get(self.native_url()+suffix,HTTP_ACCEPT_LANGUAGE=language)
                    self.assertEqual((response.status_code,response.data['code']),(409,'origin_reference_conflict'))
        self.assertEqual((ReturnReceipt.objects.count(),StockMovement.objects.filter(kind='customer_return').count()),(1,1))
        self.assertEqual(CashMovement.objects.get().amount_fcfa,5000)

    def test_live_preview_receipts_and_history_match_openapi_and_errors_are_bilingual(self):
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
        for condition in ('sellable','unsellable'):
            with self.subTest(condition=condition):
                source=self.source(condition,before=0 if condition=='sellable' else 2000);capture=self.capture(source)
                preview=self.client.get(self.url(f'operations/{capture.id}/integration-preview/'));self.assertEqual(preview.status_code,200,preview.content)
                check(preview,'/integration-preview/')
                response,_=self.apply(capture,preview.data);self.assertEqual(response.status_code,201,response.content);check(response,'/integrate/','post')
                history=self.client.get(self.native_url());self.assertEqual(history.status_code,200,history.content)
                check(history,'/returns/{return_id}/lines/{line_id}/receipts/')
        source=self.source();source.payload['receipt']['movement_id']=str(uuid.uuid4());capture=self.capture(source)
        for language,detail in (('en','Recovery physical return receipt snapshot is inconsistent.'),
                ('fr','Les données figées de la réception physique de retour récupérée sont incohérentes.')):
            with self.subTest(language=language):
                response=self.client.get(self.url(f'operations/{capture.id}/integration-preview/'),HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual((response.status_code,str(response.data['detail'])),(400,detail))
