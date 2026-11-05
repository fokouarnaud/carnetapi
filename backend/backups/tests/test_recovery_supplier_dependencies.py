"""Supplier identity, payable, advance and credit recover without fictitious trade."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import uuid
from django.db import connection,IntegrityError
from django.test import TestCase,SimpleTestCase
from backups import models as backups
from backups.tests import test_recovery_customer_dependencies as fixtures
from cash.models import CashMovement
from common.models import Activity
from identity.models import User,Membership
from purchasing.models import Supplier,SupplierDebt,SupplierCredit,SupplierLedgerEntry,PurchaseDocument,SupplierPayment,SupplierReturn
from stock.models import StockMovement


class SupplierSources:
    def supplier_source(self):
        supplier,operation,actor,device=[uuid.uuid4() for _ in range(4)]
        occurred=datetime(2026,10,24,10,tzinfo=timezone.utc)
        raw={'id':str(supplier),'operation_id':str(operation),'name':'Fournisseur survivant','phone':'+237600000034','installation_id':str(device)}
        snapshot={key:raw[key] for key in ('id','name','phone')};snapshot['version']=1
        return SimpleNamespace(operation_type='supplier_creation',operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=occurred,payload={'schema_version':1,'original_request':raw,'supplier':snapshot})

    def opening_source(self,supplier_id,kind='debt'):
        identifier,operation,actor,device=[uuid.uuid4() for _ in range(4)]
        occurred=datetime(2026,10,24,11,tzinfo=timezone.utc);amount={'debt':8000,'advance':3000,'credit':2000}[kind]
        raw={'id':str(identifier),'operation_id':str(operation),'supplier_id':str(supplier_id),'installation_id':str(device),
            'occurred_at':occurred.isoformat(),'amount_fcfa':amount,'reason':'Solde antérieur effectivement confirmé'}
        snapshot={'id':str(identifier),'supplier_id':str(supplier_id),'amount_fcfa':amount,'remaining_fcfa':amount,
            'occurred_at':occurred.isoformat(),'created_at':(occurred+timedelta(seconds=5)).isoformat()}
        if kind=='debt':snapshot['document_id']=None;key='opening_debt';operation_type='supplier_opening_debt'
        else:
            raw['balance_kind']=kind;operation_type='supplier_opening_balance';key='opening_balance'
            snapshot.update(kind='opening_'+kind,return_id=None,reference_fcfa=None,applied_to_origin_fcfa=0)
        return SimpleNamespace(operation_type=operation_type,operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=occurred,payload={'schema_version':1,'original_request':raw,key:snapshot})


class RecoverySupplierSnapshotTests(SimpleTestCase,SupplierSources):
    def test_creation_and_all_three_openings_validate_initial_facts_and_never_snapshot_a_later_balance(self):
        from backups.services.recovery_supplier_snapshots import decode_supplier_operation,RecoverySupplierSnapshotInvalid
        source=self.supplier_source();self.assertEqual(decode_supplier_operation(source).snapshot['version'],1)
        for kind in ('debt','advance','credit'):
            source=self.opening_source(uuid.uuid4(),kind);self.assertEqual(decode_supplier_operation(source).snapshot['amount_fcfa'],source.payload['original_request']['amount_fcfa'])
            for case in ('amount','remaining','id','parent','device','time','created','bool','extra','kind'):
                with self.subTest(kind=kind,case=case):
                    broken=deepcopy(source);key='opening_debt' if kind=='debt' else 'opening_balance';snapshot=broken.payload[key]
                    if case=='amount':snapshot['amount_fcfa']-=1
                    elif case=='remaining':snapshot['remaining_fcfa']-=1
                    elif case=='id':snapshot['id']=str(uuid.uuid4())
                    elif case=='parent':broken.payload['original_request']['supplier_id']=str(uuid.uuid4())
                    elif case=='device':broken.payload['original_request']['installation_id']=str(uuid.uuid4())
                    elif case=='time':snapshot['occurred_at']='2026-10-24T12:00:00Z'
                    elif case=='created':snapshot['created_at']='2026-10-24T10:00:00Z'
                    elif case=='bool':snapshot['amount_fcfa']=True
                    elif case=='extra':snapshot['cash_fcfa']=0
                    elif kind=='debt':snapshot['document_id']=str(uuid.uuid4())
                    else:snapshot['kind']='opening_credit' if kind=='advance' else 'opening_advance'
                    with self.assertRaises(RecoverySupplierSnapshotInvalid):decode_supplier_operation(broken)
        for changes in ({'version':2},{'phone':'237600000034'},{'name':' Fournisseur survivant '},{'created_at':'2026-10-24T10:00:00Z'}):
            with self.subTest(changes=changes):
                source=self.supplier_source();source.payload['supplier'].update(changes)
                with self.assertRaises(RecoverySupplierSnapshotInvalid):decode_supplier_operation(source)


class RecoverySupplierDependencyTests(TestCase,SupplierSources):
    setUp=fixtures.RecoveryCustomerDependencyTests.setUp
    authenticate=fixtures.RecoveryCustomerDependencyTests.authenticate
    generation=fixtures.RecoveryCustomerDependencyTests.generation
    record=fixtures.RecoveryCustomerDependencyTests.record
    device=fixtures.RecoveryCustomerDependencyTests.device
    review=fixtures.RecoveryCustomerDependencyTests.review
    payload=fixtures.RecoveryCustomerDependencyTests.payload
    url=fixtures.RecoveryCustomerDependencyTests.url
    capture=fixtures.RecoveryCustomerDependencyTests.capture
    preview=fixtures.RecoveryCustomerDependencyTests.preview
    apply=fixtures.RecoveryCustomerDependencyTests.apply

    def native_url(self,supplier_id=None,suffix=''):
        return f'/api/v1/shops/{self.a.id}/suppliers/'+(f'{supplier_id}/' if supplier_id else '')+suffix

    def native_body(self,source):
        return {key:value for key,value in source.payload['original_request'].items() if key not in ('supplier_id','installation_id')}

    def native_supplier(self):
        source=self.supplier_source();response=self.client.post(self.native_url(),self.native_body(source),format='json')
        self.assertEqual(response.status_code,201,response.content)
        return response.data['id']

    def native_opening(self,supplier_id,kind='debt'):
        from identity.models import DeviceSession
        source=self.opening_source(supplier_id,kind);live=DeviceSession.objects.get(installation__user=self.owner)
        source.origin_user_id=self.owner.id;source.origin_installation_id=live.installation_id
        source.payload['original_request']['installation_id']=str(live.installation_id)
        suffix='debts/' if kind=='debt' else 'opening-balances/'
        response=self.client.post(self.native_url(supplier_id,suffix),self.native_body(source),format='json')
        self.assertEqual(response.status_code,201,response.content)
        key='opening_debt' if kind=='debt' else 'opening_balance'
        for name in source.payload[key]:
            if name in response.data:source.payload[key][name]=response.data[name]
        return source,response.data

    def test_missing_supplier_recovers_before_separate_openings_without_auto_offset_trade_stock_or_cash(self):
        source=self.supplier_source();supplier_id=source.payload['supplier']['id'];parent=self.capture(source)
        sources=[self.opening_source(supplier_id,kind) for kind in ('debt','advance','credit')]
        captures=[self.capture(item,[source.operation_id]) for item in sources];old=self.preview(captures[0]);self.assertFalse(old['can_apply'])
        users=User.objects.count();response,_=self.apply(parent);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(User.objects.count(),users);self.assertFalse(SupplierLedgerEntry.objects.exists())
        self.assertEqual(self.apply(captures[0],old)[0].status_code,409)
        for item,capture,field in zip(sources,captures,('new_supplier_debt_fcfa','new_supplier_advance_fcfa','new_supplier_credit_fcfa'),strict=True):
            preview=self.preview(capture);self.assertEqual(preview[field],item.payload['original_request']['amount_fcfa'])
            self.assertEqual((preview['new_debt_fcfa'],preview['new_customer_credit_fcfa']),(0,0))
            response,command=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)
            self.assertEqual(self.client.post(self.url(f'operations/{capture.id}/integrate/'),command,format='json').data,response.data)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa,8000)
        self.assertEqual(sorted(SupplierCredit.objects.values_list('kind','remaining_fcfa')),[('opening_advance',3000),('opening_credit',2000)])
        self.assertEqual(sorted(SupplierLedgerEntry.objects.values_list('delta_fcfa',flat=True)),[-3000,-2000,8000])
        self.assertEqual((backups.RecoveredOperationOrigin.objects.count(),backups.RecoveryIntegrationDecision.objects.count()),(4,4))
        for model in (CashMovement,StockMovement,PurchaseDocument,SupplierPayment,SupplierReturn):self.assertFalse(model.objects.exists(),model.__name__)
        statement=self.client.get(self.native_url(supplier_id,'statement/'));self.assertEqual(statement.status_code,200,statement.content)
        self.assertEqual((statement.data['closing_debt_fcfa'],statement.data['closing_advance_fcfa'],statement.data['closing_credit_fcfa'],statement.data['closing_balance_fcfa']),(8000,3000,2000,3000))
        by_operation={str(item.operation_id):str(item.origin_user_id) for item in sources}
        self.assertEqual({row['operation_id']:row['actor_id'] for row in statement.data['results']},by_operation)

    def test_equal_contacts_are_distinct_and_a_new_duplicate_invalidates_preview(self):
        source=self.supplier_source();capture=self.capture(source);old=self.preview(capture)
        existing=Supplier.objects.create(shop=self.a,name=source.payload['supplier']['name'],phone=source.payload['supplier']['phone'])
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        current=self.preview(capture);self.assertTrue(current['has_possible_duplicate'])
        response,_=self.apply(capture,current);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(Supplier.objects.filter(shop=self.a).count(),2);self.assertNotEqual(response.data['business_receipt']['id'],str(existing.id))

    def test_missing_and_foreign_supplier_dependencies_never_load_private_contact_data(self):
        foreign=Supplier.objects.create(shop=self.b,name='Identité privée fournisseur B',phone='+237600000099')
        for kind in ('debt','advance','credit'):
            with self.subTest(kind=kind):
                source=self.opening_source(foreign.id,kind);capture=self.capture(source,[uuid.uuid4()]);preview=self.preview(capture)
                self.assertFalse(preview['can_apply']);self.assertIsNone(preview['current_state'])
                self.assertIn('missing_supplier',{row['code'] for row in preview['issues']});self.assertIn('missing_dependencies',{row['code'] for row in preview['issues']})
                self.assertNotIn(foreign.name,str(preview));self.assertNotIn(foreign.phone,str(preview));self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        self.assertFalse(SupplierLedgerEntry.objects.exists())

    def test_decision_failure_rolls_back_each_new_identity_balance_origin_and_ledger(self):
        source=self.supplier_source();supplier_id=source.payload['supplier']['id']
        for source in [source,*[self.opening_source(supplier_id,kind) for kind in ('debt','advance','credit')]]:
            with self.subTest(operation_type=source.operation_type,request=source.payload['original_request'].get('balance_kind')):
                capture=self.capture(source);before=(Supplier.objects.count(),SupplierDebt.objects.count(),SupplierCredit.objects.count(),SupplierLedgerEntry.objects.count(),backups.RecoveredOperationOrigin.objects.count())
                with connection.cursor() as cursor:cursor.execute("CREATE TEMP TRIGGER fail_supplier_review BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected supplier review failure'); END")
                try:
                    with self.assertRaises(IntegrityError):self.apply(capture)
                finally:
                    with connection.cursor() as cursor:cursor.execute('DROP TRIGGER fail_supplier_review')
                self.assertEqual((Supplier.objects.count(),SupplierDebt.objects.count(),SupplierCredit.objects.count(),SupplierLedgerEntry.objects.count(),backups.RecoveredOperationOrigin.objects.count()),before)
                self.assertFalse(Activity.objects.filter(id=source.operation_id).exists())
                self.assertEqual(self.apply(capture)[0].status_code,201)

    def test_native_openings_are_known_after_allocation_and_refund_without_resetting_current_balances(self):
        supplier_id=self.native_supplier();debt,first_debt=self.native_opening(supplier_id)
        advance,first_advance=self.native_opening(supplier_id,'advance');credit,first_credit=self.native_opening(supplier_id,'credit')
        allocation={'operation_id':str(uuid.uuid4()),'reason':'Affectation ultérieure réelle','allocations':[{'debt_id':first_debt['id'],'amount_fcfa':1000}]}
        allocated=self.client.post(self.native_url(supplier_id,f"credits/{first_advance['id']}/allocate/"),allocation,format='json');self.assertEqual(allocated.status_code,201,allocated.content)
        refund={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':'2026-10-25T12:00:00Z','reason':'Remboursement réel reçu','method':'cash','amount_fcfa':500}
        refunded=self.client.post(self.native_url(supplier_id,f"credits/{first_credit['id']}/refund/"),refund,format='json');self.assertEqual(refunded.status_code,201,refunded.content)
        for source,first in ((debt,first_debt),(advance,first_advance),(credit,first_credit)):
            capture=self.capture(source);preview=self.preview(capture);self.assertEqual(preview['outcome'],'already_known',preview)
            self.assertTrue(preview['origin_installation_verified']);response,_=self.apply(capture,preview)
            self.assertEqual((response.status_code,response.data['business_receipt']),(201,first))
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa,7000)
        self.assertEqual(sorted(SupplierCredit.objects.values_list('remaining_fcfa',flat=True)),[1500,2000])
        self.assertEqual((CashMovement.objects.get().amount_fcfa,backups.RecoveredOperationOrigin.objects.count()),(500,0))

    def test_captured_uuids_and_current_rights_are_checked_before_parsing_or_native_write(self):
        supplier_id=self.native_supplier();sources=[self.supplier_source(),self.opening_source(supplier_id),self.opening_source(supplier_id,'advance')]
        captures=[self.capture(source) for source in sources]
        seller=User.objects.create_user(username='supplier-recovery-seller');member=Membership.objects.create(shop=self.a,user=seller)
        self.review(self.a,seller,'seller',membership=member,confirmed=True);api=self.authenticate(seller)
        for source,capture in zip(sources,captures,strict=True):
            self.assertEqual(api.generic('POST',self.url(f'operations/{capture.id}/integrate/'),b'invalid',content_type='application/json').status_code,403)
            path=self.native_url() if source.operation_type=='supplier_creation' else self.native_url(supplier_id,'debts/' if source.operation_type=='supplier_opening_debt' else 'opening-balances/')
            self.assertEqual(self.client.post(path,self.native_body(source),format='json').status_code,409)
            self.assertEqual(self.client.get(self.url(f'operations/{capture.id}/integration-preview/',shop=self.b)).status_code,404)
        capture=captures[1];preview=self.preview(capture)
        review=backups.RestoreAccessReview.objects.get(generation=self.current,shop=self.a,user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=1,version=2,state='refused',reason='Qualité actuelle retirée')
        self.assertEqual(self.apply(capture,preview)[0].status_code,404);self.assertFalse(SupplierDebt.objects.exists())

    def test_known_supplier_keeps_initial_contact_without_claiming_a_proven_server_creation_time(self):
        from identity.models import DeviceSession
        source=self.supplier_source();live=DeviceSession.objects.get(installation__user=self.owner)
        source.origin_user_id=self.owner.id;source.origin_installation_id=live.installation_id
        source.payload['original_request']['installation_id']=str(live.installation_id)
        response=self.client.post(self.native_url(),self.native_body(source),format='json');self.assertEqual(response.status_code,201,response.content)
        first=response.data
        Supplier.objects.filter(id=first['id']).update(name='Contact corrigé ultérieurement',version=2)
        capture=self.capture(source);preview=self.preview(capture);self.assertEqual(preview['outcome'],'already_known',preview)
        self.assertTrue(preview['origin_installation_verified'])
        self.assertIn('declared_supplier_creation_time',{row['code'] for row in preview['notices']})
        response,_=self.apply(capture);self.assertEqual(response.data['business_receipt'],first)
        self.assertEqual(Supplier.objects.get().name,'Contact corrigé ultérieurement')

    def test_recovered_openings_survive_consumption_and_next_generation_without_old_balances(self):
        source=self.supplier_source();supplier_id=source.payload['supplier']['id'];profile=self.capture(source)
        self.assertEqual(self.apply(profile)[0].status_code,201)
        openings=[self.opening_source(supplier_id,kind) for kind in ('debt','advance','credit')];first=[]
        for opening in openings:
            response,_=self.apply(self.capture(opening));self.assertEqual(response.status_code,201,response.content);first.append(response.data['business_receipt'])
        allocated=self.client.post(self.native_url(supplier_id,f"credits/{first[1]['id']}/allocate/"),
            {'operation_id':str(uuid.uuid4()),'reason':'Affectation après reprise','allocations':[{'debt_id':first[0]['id'],'amount_fcfa':1000}]},format='json')
        self.assertEqual(allocated.status_code,201,allocated.content)
        refunded=self.client.post(self.native_url(supplier_id,f"credits/{first[2]['id']}/refund/"),
            {'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':'2026-10-25T12:00:00Z','reason':'Reçu après reprise','method':'orange','amount_fcfa':500},format='json')
        self.assertEqual(refunded.status_code,201,refunded.content)
        self.current.current_slot=None;self.current.save();self.current=self.generation();self.registry=self.record(self.current)
        self.review(self.a,self.owner,'owner',confirmed=True)
        for opening,expected in zip(openings,first,strict=True):
            capture=self.capture(opening);preview=self.preview(capture);self.assertEqual(preview['outcome'],'already_known',preview)
            response,_=self.apply(capture,preview);self.assertEqual(response.data['business_receipt'],expected)
            self.assertNotEqual(expected['created_at'],Activity.objects.get(id=opening.operation_id).details['recovery']['source_snapshot']['created_at'])
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa,7000)
        self.assertEqual(sorted(SupplierCredit.objects.values_list('remaining_fcfa',flat=True)),[1500,2000])
        self.assertEqual((SupplierLedgerEntry.objects.count(),CashMovement.objects.count(),backups.RecoveredOperationOrigin.objects.count()),(5,1,4))

    def test_known_opening_requires_immutable_model_ledger_and_first_acknowledgement(self):
        supplier_id=self.native_supplier()
        for kind,case in (('debt','model'),('advance','ledger'),('credit','receipt')):
            with self.subTest(kind=kind,case=case):
                source=self.opening_source(supplier_id,kind);capture=self.capture(source);response,_=self.apply(capture)
                self.assertEqual(response.status_code,201,response.content)
                if case=='model':SupplierDebt.objects.filter(id=response.data['business_receipt']['id']).update(amount_fcfa=9000)
                elif case=='ledger':SupplierLedgerEntry.objects.filter(operation_id=source.operation_id).update(source_id=uuid.uuid4())
                else:
                    operation=Activity.objects.get(id=source.operation_id);operation.details['response']['remaining_fcfa']=1;operation.save(update_fields=['details','updated_at'])
                preview=self.preview(capture);self.assertFalse(preview['can_apply'],preview)
                self.assertIn('operation_conflict',{row['code'] for row in preview['issues']});self.assertEqual(self.apply(capture,preview)[0].status_code,409)

    def test_native_credit_history_and_statement_project_source_actor_and_reject_foreign_lineage(self):
        supplier_id=self.native_supplier();source=self.opening_source(supplier_id,'credit');capture=self.capture(source)
        response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content);identifier=response.data['business_receipt']['id']
        paths=[('opening-balances/','list'),('credits/','list'),(f'credits/{identifier}/','detail'),(f'credits/{identifier}/history/','list'),('statement/','list')]
        for suffix,kind in paths:
            response=self.client.get(self.native_url(supplier_id,suffix));self.assertEqual(response.status_code,200,response.content)
            row=response.data['results'][0] if kind=='list' else response.data
            self.assertEqual(row['actor_id'],str(source.origin_user_id))
        backups.RecoveryJournalOperation.objects.filter(id=capture.id).update(shop=self.b)
        for language in ('en','fr'):
            for suffix,_kind in paths:
                response=self.client.get(self.native_url(supplier_id,suffix),HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual((response.status_code,response.data['code']),(409,'origin_reference_conflict'))
        self.assertEqual(SupplierCredit.objects.get().remaining_fcfa,2000)

    def test_missing_identity_evidence_future_dates_and_uuid_conflicts_remain_captured(self):
        from identity.models import DeviceSession
        supplier_id=self.native_supplier()
        for case,code in (('device','origin_identity_conflict'),('future','future_effective_date'),('entity','entity_uuid_conflict'),('operation','operation_conflict')):
            source=self.opening_source(supplier_id)
            if case=='device':
                source.origin_installation_id=DeviceSession.objects.get(installation__user=self.owner).installation_id
                source.payload['original_request']['installation_id']=str(source.origin_installation_id)
            elif case=='future':source.payload['opening_debt']['created_at']='2099-01-01T00:00:00Z'
            elif case=='entity':SupplierDebt.objects.create(id=source.payload['opening_debt']['id'],shop=self.b,
                supplier=Supplier.objects.create(shop=self.b,name='Autre'),operation=Activity.objects.create(shop=self.b,actor=self.owner,action='opening',target_id=uuid.uuid4()),
                installation_id=DeviceSession.objects.get(installation__user=self.owner).installation_id,occurred_at=source.occurred_at,amount_fcfa=1,remaining_fcfa=1)
            else:Activity.objects.create(id=source.operation_id,shop=self.b,actor=self.owner,action='other',target_id=uuid.uuid4(),details={'private':'B secret'})
            capture=self.capture(source);preview=self.preview(capture);self.assertFalse(preview['can_apply'])
            self.assertIn(code,{row['code'] for row in preview['issues']});self.assertNotIn('B secret',str(preview));self.assertEqual(self.apply(capture,preview)[0].status_code,409)

    def test_live_json_matches_openapi_and_invalid_source_errors_are_bilingual(self):
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
        source=self.supplier_source();supplier_id=source.payload['supplier']['id']
        for source in [source,*[self.opening_source(supplier_id,kind) for kind in ('debt','advance','credit')]]:
            capture=self.capture(source);preview=self.client.get(self.url(f'operations/{capture.id}/integration-preview/'))
            self.assertEqual(preview.status_code,200,preview.content);check(preview,'/integration-preview/')
            response,_=self.apply(capture,preview.data);self.assertEqual(response.status_code,201,response.content);check(response,'/integrate/','post')
        for suffix,ending in (('statement/','/suppliers/{supplier_id}/statement/'),('credits/','/suppliers/{supplier_id}/credits/'),
                ('opening-balances/','/suppliers/{supplier_id}/opening-balances/')):
            response=self.client.get(self.native_url(supplier_id,suffix));self.assertEqual(response.status_code,200,response.content);check(response,ending)
        source=self.opening_source(supplier_id);source.payload['opening_debt']['remaining_fcfa']=1;capture=self.capture(source)
        for language,detail in (('en','Recovery supplier or opening-balance snapshot is inconsistent.'),('fr','Les données figées du fournisseur ou de son solde d’ouverture sont incohérentes.')):
            response=self.client.get(self.url(f'operations/{capture.id}/integration-preview/'),HTTP_ACCEPT_LANGUAGE=language)
            self.assertEqual((response.status_code,str(response.data['detail'])),(400,detail))

    def test_foreign_native_activity_is_refused_before_its_private_details_are_loaded(self):
        from django.test.utils import CaptureQueriesContext
        supplier_id=self.native_supplier();source,receipt=self.native_opening(supplier_id,'credit')
        foreign=Activity.objects.create(shop=self.b,actor=self.technical,action='supplier_balance_opened',target_id=uuid.uuid4(),
            reason='Motif technique privé B',details={'private':'Justificatif confidentiel B'})
        SupplierCredit.objects.filter(id=receipt['id']).update(operation=foreign)
        SupplierLedgerEntry.objects.filter(operation_id=source.operation_id).update(operation=foreign)
        for suffix in ('opening-balances/','credits/',f"credits/{receipt['id']}/",f"credits/{receipt['id']}/history/",'statement/'):
            with self.subTest(suffix=suffix):
                with CaptureQueriesContext(connection) as queries:response=self.client.get(self.native_url(supplier_id,suffix))
                self.assertEqual((response.status_code,response.data.get('code')),(409,'origin_reference_conflict'),response.content)
                sensitive=[row['sql'] for row in queries.captured_queries if '"core_activity"."details"' in row['sql']]
                self.assertEqual(sensitive,[],sensitive)
                self.assertNotIn('Justificatif confidentiel B',str(response.data));self.assertNotIn('Motif technique privé B',str(response.data))
