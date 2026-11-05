"""Recovered cancellation and its compensating return commit as one reviewed transaction."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import uuid
from django.db import connection,IntegrityError
from django.test import TestCase,SimpleTestCase
from backups import models as backups
from backups.tests import test_recovery_sale_returns as fixtures
from common.models import Activity
from common.services.operations import json_value
from cash.models import CashMovement
from customers.models import CustomerDebt,CustomerLedgerEntry
from sales.models import SaleCancellation,SaleReturn,SaleReturnLine
from sales.services.return_pricing import return_amount_fcfa
from stock.models import StockMovement,ReturnReceipt


class CancellationSources:
    def source(self,sale=None,*,before=None,debt_remaining=None,with_child=True):
        sale=sale or self.sale;before=before or {}
        operation,actor,device,cancelled=[uuid.uuid4() for _ in range(4)]
        occurred=datetime(2026,10,25,10,tzinfo=timezone.utc);reason='Annulation réellement confirmée'
        raw={'id':str(cancelled),'operation_id':str(operation),'sale_id':sale['id'],'installation_id':str(device),
            'occurred_at':occurred.isoformat(),'reason':reason}
        child=None
        if with_child:
            child=fixtures.ReturnSources.source(self,sale)
            child.operation_id=uuid.uuid5(cancelled,'cancellation-return-operation');child.origin_user_id=actor
            child.origin_installation_id=device;child.occurred_at=occurred
            requested=child.payload['original_request'];frozen=child.payload['sale_return'];return_id=uuid.uuid5(cancelled,'cancellation-return')
            requested.update(id=str(return_id),operation_id=str(child.operation_id),installation_id=str(device),occurred_at=occurred.isoformat(),reason=reason)
            frozen.update(id=str(return_id),occurred_at=occurred.isoformat(),created_at=(occurred+timedelta(seconds=4)).isoformat())
            kept=[]
            for row in frozen['lines']:
                old=before.get(row['sale_line_id'],0);remaining=row['original_quantity_milli']-old
                if not remaining:continue
                row.update(id=str(uuid.uuid5(cancelled,'cancellation-line:'+row['sale_line_id'])),quantity_milli=remaining,
                    amount_fcfa=return_amount_fcfa(row['original_amount_fcfa'],row['original_quantity_milli'],old,remaining));kept.append(row)
            frozen['lines']=kept;requested['lines']=[{key:row[key] for key in ('id','sale_line_id','quantity_milli')} for row in kept]
            total=sum(row['amount_fcfa'] for row in kept)
            debt=debt_remaining if debt_remaining is not None else sale.get('debt',{}).get('remaining_fcfa',0) if sale.get('debt') else 0
            reduced=min(total,debt);frozen.update(amount_fcfa=total,debt_reduced_fcfa=reduced,credit_fcfa=total-reduced)
        snapshot={'id':str(cancelled),'sale_id':sale['id'],'sale_return_id':child.payload['sale_return']['id'] if child else None,
            'occurred_at':occurred.isoformat(),'created_at':(occurred+timedelta(seconds=5)).isoformat(),
            'compensating_return':deepcopy(child.payload) if child else None}
        return SimpleNamespace(operation_type='sale_cancellation',operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=occurred,payload={'schema_version':1,'original_request':raw,'cancellation':snapshot},child=child)


class RecoveryCancellationSnapshotTests(SimpleTestCase,CancellationSources):
    def setUp(self):
        self.sale={'id':str(uuid.uuid4()),'customer_id':str(uuid.uuid4()),'debt':None,
            'lines':[{'id':str(uuid.uuid4()),'base_quantity_milli':10000,'net_fcfa':5000,'unit_kind':'piece'}]}

    def test_parent_and_generated_child_are_bound_by_ids_actor_time_reason_and_frozen_lines(self):
        from backups.services.recovery_sale_cancellations import decode_sale_cancellation,RecoveryCancellationInvalid
        self.assertIsNotNone(decode_sale_cancellation(self.source()).child)
        self.assertIsNone(decode_sale_cancellation(self.source(with_child=False)).child)
        for case in ('id','return','child_operation','line','time','reason','missing','extra','child_created_later'):
            with self.subTest(case=case):
                source=self.source();snapshot=source.payload['cancellation'];child=snapshot['compensating_return']
                if case=='id':snapshot['id']=str(uuid.uuid4())
                elif case=='return':snapshot['sale_return_id']=str(uuid.uuid4())
                elif case=='child_operation':child['original_request']['operation_id']=str(uuid.uuid4())
                elif case=='line':
                    identifier=str(uuid.uuid4());child['sale_return']['lines'][0]['id']=identifier;child['original_request']['lines'][0]['id']=identifier
                elif case=='time':child['sale_return']['occurred_at']='2026-10-25T11:00:00Z'
                elif case=='reason':child['original_request']['reason']='Autre événement'
                elif case=='missing':snapshot['compensating_return']=None
                elif case=='child_created_later':child['sale_return']['created_at']='2026-10-25T10:00:06Z'
                else:snapshot['refund_fcfa']=5000
                with self.assertRaises(RecoveryCancellationInvalid):decode_sale_cancellation(source)


class RecoverySaleCancellationTests(TestCase,CancellationSources):
    setUp=fixtures.RecoverySaleReturnTests.setUp
    prepare_sale=fixtures.RecoverySaleReturnTests.prepare_sale
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

    def native_url(self,sale=None):return f"/api/v1/shops/{self.a.id}/sales/{(sale or self.sale)['id']}/cancellation/"

    def capture_pair(self,source):
        parent=self.capture(source,[source.child.operation_id] if source.child else [])
        child=self.capture(source.child) if source.child else None
        return parent,child

    def test_parent_and_child_apply_atomically_with_two_origins_and_linked_reviews_without_stock_or_money(self):
        source=self.source();parent=self.capture(source,[source.child.operation_id]);old=self.preview(parent)
        self.assertFalse(old['can_apply']);child=self.capture(source.child)
        self.assertEqual(self.apply(parent,old)[0].status_code,409)
        response,command=self.apply(parent);self.assertEqual(response.status_code,201,response.content)
        receipt=response.data['business_receipt'];self.assertEqual(receipt['actor_id'],str(source.origin_user_id))
        self.assertEqual(receipt['sale_return_id'],source.child.payload['sale_return']['id'])
        self.assertEqual((SaleCancellation.objects.count(),SaleReturn.objects.count(),SaleReturnLine.objects.count()),(1,1,1))
        self.assertEqual((backups.RecoveredOperationOrigin.objects.count(),backups.RecoveryIntegrationDecision.objects.count()),(2,2))
        self.assertEqual(response.data['related_integration_decision_ids'],[str(child.integration_decisions.get().id)])
        self.assertEqual((StockMovement.objects.count(),ReturnReceipt.objects.count(),CashMovement.objects.get().amount_fcfa),(2,0,5000))
        self.assertEqual(SaleReturn.objects.get().credit_fcfa,5000)
        self.assertEqual(self.client.post(self.url(f'operations/{parent.id}/integrate/'),command,format='json').data,response.data)
        known=self.preview(parent);self.assertEqual(known['outcome'],'already_known')
        ack,_=self.apply(parent,known);self.assertEqual(ack.data['business_receipt'],receipt)
        self.assertEqual(SaleReturn.objects.count(),1)
        history=self.client.get(self.native_url());self.assertEqual(history.status_code,200,history.content)
        self.assertEqual(history.data['actor_id'],str(source.origin_user_id))

    def test_credit_cancellation_reduces_only_its_sale_debt_and_preserves_original_payments(self):
        sale=self.prepare_sale(mode='credit',payments=[{'id':str(uuid.uuid4()),'method':'cash','amount_fcfa':2000}])
        source=self.source(sale);parent,_=self.capture_pair(source)
        response,_=self.apply(parent);self.assertEqual(response.status_code,201,response.content)
        returned=SaleReturn.objects.get();self.assertEqual((returned.amount_fcfa,returned.debt_reduced_fcfa,returned.credit_fcfa),(5000,3000,2000))
        self.assertEqual(CustomerDebt.objects.get(sale_id=sale['id']).remaining_fcfa,0)
        self.assertEqual(sorted(CashMovement.objects.values_list('amount_fcfa',flat=True)),[2000,5000])
        self.assertEqual(list(CustomerDebt.objects.filter(sale__isnull=True).order_by('amount_fcfa').values_list('remaining_fcfa',flat=True)),[3000,4000])

    def test_partial_return_then_cancellation_preserves_33_and_67_without_repricing(self):
        sale=self.prepare_sale(global_discount_fcfa=1400,lines=[{'id':str(uuid.uuid4()),'product_id':str(self.product.id),
            'product_version':self.product.version,'quantity_milli':3000}],payments=[{'id':str(uuid.uuid4()),'method':'cash','amount_fcfa':100}])
        raw={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':'2026-10-24T12:00:00Z','reason':'Première pièce retournée',
            'lines':[{'id':str(uuid.uuid4()),'sale_line_id':sale['lines'][0]['id'],'quantity_milli':1000}]}
        previous=self.client.post(f"/api/v1/shops/{self.a.id}/sales/{sale['id']}/returns/",raw,format='json');self.assertEqual(previous.status_code,201,previous.content)
        source=self.source(sale,before={sale['lines'][0]['id']:1000});parent,_=self.capture_pair(source)
        self.product.sale_price_fcfa=9999;self.product.is_active=False;self.product.save()
        response,_=self.apply(parent);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(sorted(SaleReturn.objects.values_list('amount_fcfa',flat=True)),[33,67])
        self.assertEqual(sorted(SaleReturnLine.objects.values_list('quantity_milli',flat=True)),[1000,2000])
        self.assertEqual((StockMovement.objects.count(),ReturnReceipt.objects.count()),(3,0))

    def test_fully_returned_sale_with_no_child_is_marked_cancelled_without_another_return(self):
        previous=fixtures.ReturnSources.source(self)
        raw={key:value for key,value in previous.payload['original_request'].items() if key not in ('sale_id','installation_id')}
        response=self.client.post(f"/api/v1/shops/{self.a.id}/sales/{self.sale['id']}/returns/",raw,format='json');self.assertEqual(response.status_code,201,response.content)
        source=self.source(with_child=False);parent=self.capture(source)
        response,_=self.apply(parent);self.assertEqual(response.status_code,201,response.content);self.assertIsNone(response.data['business_receipt']['sale_return_id'])
        self.assertEqual((SaleReturn.objects.count(),SaleCancellation.objects.count(),StockMovement.objects.count()),(1,1,2))
        self.assertEqual(CustomerLedgerEntry.objects.count(),3)

    def test_changed_child_financial_context_blocks_whole_cancellation(self):
        sale=self.prepare_sale(mode='credit',payments=[{'id':str(uuid.uuid4()),'method':'cash','amount_fcfa':2000}])
        source=self.source(sale);parent,child=self.capture_pair(source);old=self.preview(parent)
        CustomerDebt.objects.filter(sale_id=sale['id']).update(remaining_fcfa=2500)
        self.assertEqual(self.apply(parent,old)[0].status_code,409)
        preview=self.preview(parent);self.assertFalse(preview['can_apply']);self.assertEqual(self.apply(parent,preview)[0].status_code,409)
        self.assertFalse(SaleCancellation.objects.exists());self.assertFalse(SaleReturn.objects.exists())
        self.assertFalse(child.integration_decisions.exists())

    def test_parent_review_failure_rolls_back_child_review_origin_return_debt_and_ledger(self):
        source=self.source();parent,child=self.capture_pair(source);preview=self.preview(parent)
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER fail_cancellation_review BEFORE INSERT ON backups_recoveryintegrationdecision WHEN NEW.capture_id='"+parent.id.hex+"' BEGIN SELECT RAISE(ABORT,'injected parent review failure'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(parent,preview)
        finally:
            with connection.cursor() as cursor:cursor.execute('DROP TRIGGER fail_cancellation_review')
        self.assertFalse(SaleReturn.objects.exists());self.assertFalse(SaleCancellation.objects.exists());self.assertFalse(child.integration_decisions.exists())
        self.assertFalse(backups.RecoveredOperationOrigin.objects.exists());self.assertEqual(CustomerLedgerEntry.objects.count(),2)
        self.assertFalse(Activity.objects.filter(id__in=[source.operation_id,source.child.operation_id]).exists())

    def test_native_cancellation_and_child_are_recognized_without_separate_child_capture(self):
        source=self.source();source.origin_user_id=self.owner.id;source.origin_installation_id=self.live.installation_id
        source.payload['original_request']['installation_id']=str(self.live.installation_id)
        nested=source.payload['cancellation']['compensating_return'];nested['original_request']['installation_id']=str(self.live.installation_id)
        raw={key:value for key,value in source.payload['original_request'].items() if key not in ('sale_id','installation_id')}
        native=self.client.post(self.native_url(),raw,format='json');self.assertEqual(native.status_code,201,native.content)
        for key in native.data:
            if key in source.payload['cancellation']:source.payload['cancellation'][key]=native.data[key]
        child=self.client.get(f"/api/v1/shops/{self.a.id}/sales/{self.sale['id']}/returns/{native.data['sale_return_id']}/")
        self.assertEqual(child.status_code,200,child.content)
        for key in child.data:
            if key in nested['sale_return']:nested['sale_return'][key]=child.data[key]
        capture=self.capture(source);preview=self.preview(capture)
        self.assertEqual(preview['outcome'],'already_known',preview);self.assertTrue(preview['origin_installation_verified'])
        response,_=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.data['business_receipt'],native.data);self.assertEqual(response.data['related_integration_decision_ids'],[])
        self.assertEqual((SaleCancellation.objects.count(),SaleReturn.objects.count(),backups.RecoveredOperationOrigin.objects.count()),(1,1,0))

    def test_preintegrated_child_and_next_generation_keep_claim_and_first_acknowledgements(self):
        from customers.services.return_credit_balance import remaining_return_credit
        source=self.source();parent,child=self.capture_pair(source)
        response,_=self.apply(child);self.assertEqual(response.status_code,201,response.content)
        fixtures.RecoverySaleReturnTests.refund(self,response.data['business_receipt']['id'])
        response,_=self.apply(parent);self.assertEqual(response.status_code,201,response.content);first=response.data['business_receipt']
        self.current.current_slot=None;self.current.save();self.current=self.generation();self.registry=self.record(self.current)
        self.review(self.a,self.owner,'owner',confirmed=True)
        parent=self.capture(source);known=self.preview(parent);self.assertEqual(known['outcome'],'already_known',known)
        response,_=self.apply(parent,known);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.data['business_receipt'],first)
        self.assertEqual(remaining_return_credit(SaleReturn.objects.get()),4500)
        self.assertEqual((SaleCancellation.objects.count(),SaleReturn.objects.count(),CashMovement.objects.count()),(1,1,2))
        operation=Activity.objects.get(id=source.operation_id)
        self.assertEqual(operation.details['recovery']['source_snapshot']['created_at'],'2026-10-25T10:00:05Z')
        self.assertNotEqual(first['created_at'],operation.details['recovery']['source_snapshot']['created_at'])

    def test_zero_value_and_anonymous_cancellations_still_preserve_physical_quantity(self):
        for anonymous,price,discount in ((True,5000,0),(False,0,5000)):
            with self.subTest(anonymous=anonymous):
                sale=self.prepare_sale(customer_id=None if anonymous else str(self.customer.id),global_discount_fcfa=discount,
                    payments=[{'id':str(uuid.uuid4()),'method':'cash','amount_fcfa':price}] if price else [])
                before=(CustomerLedgerEntry.objects.count(),CashMovement.objects.count(),StockMovement.objects.count())
                source=self.source(sale);parent,_=self.capture_pair(source);response,_=self.apply(parent)
                self.assertEqual(response.status_code,201,response.content)
                returned=SaleReturn.objects.get(id=source.child.payload['sale_return']['id'])
                self.assertEqual((returned.amount_fcfa,returned.lines.get().quantity_milli),(price,10000))
                self.assertEqual((CustomerLedgerEntry.objects.count(),CashMovement.objects.count(),StockMovement.objects.count()),
                    (before[0]+(not anonymous),before[1],before[2]))

    def test_missing_child_null_remainder_dependencies_cycles_and_different_copy_are_blocked(self):
        for case,code in (('null','cancellation_remainder_conflict'),('dependency','missing_dependencies'),
                ('cycle','cancellation_child_dependency_cycle'),('copy','cancellation_child_copy_conflict')):
            with self.subTest(case=case):
                source=self.source(with_child=case!='null')
                parent=self.capture(source,[uuid.uuid4()] if case=='dependency' else [])
                if source.child:
                    if case=='copy':source.child.payload['original_request']['reason']='Autre copie'
                    self.capture(source.child,[source.operation_id] if case=='cycle' else [])
                preview=self.preview(parent);self.assertFalse(preview['can_apply'],preview)
                self.assertIn(code,{row['code'] for row in preview['issues']})
                self.assertEqual(self.apply(parent,preview)[0].status_code,409)
        self.assertFalse(SaleCancellation.objects.exists());self.assertFalse(SaleReturn.objects.exists())

    def test_actual_foreign_sale_and_corrupt_origin_do_not_leak_into_authorized_reads(self):
        from sales.models import Sale
        foreign=self.prepare_sale();Sale.objects.filter(id=foreign['id']).update(shop=self.b)
        source=self.source(foreign);parent,_=self.capture_pair(source);preview=self.preview(parent)
        self.assertIsNone(preview['current_state']);self.assertFalse(preview['can_apply'])
        self.assertIn('missing_sale',{row['code'] for row in preview['issues']})
        source=self.source();parent,_=self.capture_pair(source);self.assertEqual(self.apply(parent)[0].status_code,201)
        backups.RecoveryJournalOperation.objects.filter(id=parent.id).update(shop=self.b)
        for language in ('fr','en'):
            response=self.client.get(self.native_url(),HTTP_ACCEPT_LANGUAGE=language)
            self.assertEqual((response.status_code,response.data['code']),(409,'origin_reference_conflict'))
        self.assertEqual((SaleCancellation.objects.count(),SaleReturn.objects.count()),(1,1))

    def test_current_rights_and_captured_parent_and_child_cannot_bypass_review(self):
        from identity.models import User,Membership
        source=self.source();parent,child=self.capture_pair(source)
        seller=User.objects.create_user(username='cancellation-seller');member=Membership.objects.create(shop=self.a,user=seller)
        self.review(self.a,seller,'seller',membership=member,confirmed=True);api=self.authenticate(seller)
        self.assertEqual(api.generic('POST',self.url(f'operations/{parent.id}/integrate/'),b'invalid',content_type='application/json').status_code,403)
        raw={key:value for key,value in source.payload['original_request'].items() if key not in ('sale_id','installation_id')}
        self.assertEqual(self.client.post(self.native_url(),raw,format='json').status_code,409)
        raw={key:value for key,value in source.child.payload['original_request'].items() if key not in ('sale_id','installation_id')}
        self.assertEqual(self.client.post(f"/api/v1/shops/{self.a.id}/sales/{self.sale['id']}/returns/",raw,format='json').status_code,409)
        self.assertEqual(self.client.get(self.url(f'operations/{parent.id}/integration-preview/',shop=self.b)).status_code,404)
        preview=self.preview(parent)
        review=backups.RestoreAccessReview.objects.get(generation=self.current,shop=self.a,user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=1,version=2,state='refused',reason='Qualité actuelle retirée')
        self.assertEqual(self.apply(parent,preview)[0].status_code,404)
        self.assertFalse(SaleCancellation.objects.exists());self.assertFalse(child.integration_decisions.exists())

    def test_live_bodies_match_openapi_and_invalid_snapshots_are_bilingual(self):
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
        source=self.source();parent,_=self.capture_pair(source)
        preview=self.client.get(self.url(f'operations/{parent.id}/integration-preview/'));self.assertEqual(preview.status_code,200,preview.content);check(preview,'/integration-preview/')
        response,_=self.apply(parent,preview.data);self.assertEqual(response.status_code,201,response.content);check(response,'/integrate/','post')
        history=self.client.get(self.native_url());self.assertEqual(history.status_code,200,history.content);check(history,'/sales/{sale_id}/cancellation/')
        source=self.source();source.payload['cancellation']['id']=str(uuid.uuid4());parent=self.capture(source)
        for language,detail in (('en','Recovery sale cancellation snapshot is inconsistent.'),('fr',"Les données figées de l'annulation de vente récupérée sont incohérentes.")):
            response=self.client.get(self.url(f'operations/{parent.id}/integration-preview/'),HTTP_ACCEPT_LANGUAGE=language)
            self.assertEqual((response.status_code,str(response.data['detail'])),(400,detail))

    def test_child_tampering_and_late_return_require_new_control_without_partial_cancellation(self):
        source=self.source();parent,child=self.capture_pair(source);old=self.preview(parent)
        altered=deepcopy(child.payload);altered['original_request']['reason']='Altération après capture'
        backups.RecoveryJournalOperation.objects.filter(id=child.id).update(payload=altered)
        self.assertEqual(self.apply(parent,old)[0].status_code,409)
        self.assertEqual(self.client.get(self.url(f'operations/{parent.id}/integration-preview/')).status_code,409)
        backups.RecoveryJournalOperation.objects.filter(id=child.id).update(payload=source.child.payload)
        returned=fixtures.ReturnSources.source(self)
        raw={key:value for key,value in returned.payload['original_request'].items() if key not in ('sale_id','installation_id')}
        raw['occurred_at']='2026-10-26T10:00:00Z'
        response=self.client.post(f"/api/v1/shops/{self.a.id}/sales/{self.sale['id']}/returns/",raw,format='json')
        self.assertEqual(response.status_code,201,response.content)
        preview=self.preview(parent);self.assertFalse(preview['can_apply'])
        self.assertIn('cancellation_timeline_conflict',{row['code'] for row in preview['issues']})
        self.assertEqual(self.apply(parent,preview)[0].status_code,409)
        self.assertFalse(SaleCancellation.objects.exists());self.assertFalse(child.integration_decisions.exists())

    def test_credit_parent_failure_restores_original_debt_and_no_child_review(self):
        sale=self.prepare_sale(mode='credit',payments=[{'id':str(uuid.uuid4()),'method':'cash','amount_fcfa':2000}])
        source=self.source(sale);parent,child=self.capture_pair(source);preview=self.preview(parent)
        ledger=CustomerLedgerEntry.objects.count()
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER fail_credit_cancel BEFORE INSERT ON backups_recoveryintegrationdecision WHEN NEW.capture_id='"+parent.id.hex+"' BEGIN SELECT RAISE(ABORT,'injected credit cancellation failure'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(parent,preview)
        finally:
            with connection.cursor() as cursor:cursor.execute('DROP TRIGGER fail_credit_cancel')
        self.assertEqual(CustomerDebt.objects.get(sale_id=sale['id']).remaining_fcfa,3000)
        self.assertEqual(CustomerLedgerEntry.objects.count(),ledger)
        self.assertFalse(SaleReturn.objects.exists());self.assertFalse(SaleCancellation.objects.exists())
        self.assertFalse(child.integration_decisions.exists());self.assertFalse(backups.RecoveredOperationOrigin.objects.exists())
