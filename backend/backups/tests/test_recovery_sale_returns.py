"""Lost commercial returns retain billed values and never receive stock or refund cash."""
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import uuid
from django.db import connection,IntegrityError
from django.test import TestCase,SimpleTestCase
from backups import models as backups
from backups.tests import test_recovery_customer_allocations as fixtures
from common.models import Activity
from cash.models import CashMovement
from customers.models import CustomerDebt,CustomerLedgerEntry
from sales.models import SaleReturn,SaleReturnLine
from stock.models import StockMovement,ReturnReceipt


class ReturnSources:
    def source(self,sale=None):
        sale=sale or self.sale
        operation,actor,device,returned=[uuid.uuid4() for _ in range(4)]
        occurred=datetime(2026,10,24,11,tzinfo=timezone.utc)
        lines=[{"id":str(uuid.uuid4()),"sale_line_id":row["id"],"quantity_milli":row["base_quantity_milli"],
            "amount_fcfa":row["net_fcfa"],"original_quantity_milli":row["base_quantity_milli"],
            "original_amount_fcfa":row["net_fcfa"],"unit_kind":row["unit_kind"]} for row in sale["lines"]]
        total=sum(row["amount_fcfa"] for row in lines);reduced=min(total,sale["debt"]["remaining_fcfa"]) if sale.get("debt") else 0
        raw={"id":str(returned),"operation_id":str(operation),"occurred_at":occurred.isoformat(),"reason":"Retour commercial réellement accepté",
            "sale_id":sale["id"],"installation_id":str(device),"lines":[{key:row[key] for key in ("id","sale_line_id","quantity_milli")} for row in lines]}
        snapshot={"id":str(returned),"sale_id":sale["id"],"customer_id":sale.get("customer_id"),"amount_fcfa":total,
            "debt_reduced_fcfa":reduced,"credit_fcfa":total-reduced,"occurred_at":occurred.isoformat(),
            "created_at":(occurred+timedelta(seconds=5)).isoformat(),"lines":lines}
        return SimpleNamespace(operation_type="sale_return",operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=occurred,payload={"schema_version":1,"original_request":raw,"sale_return":snapshot})


class RecoverySaleReturnSnapshotTests(SimpleTestCase,ReturnSources):
    def setUp(self):
        self.sale={"id":str(uuid.uuid4()),"customer_id":str(uuid.uuid4()),"debt":None,
            "lines":[{"id":str(uuid.uuid4()),"base_quantity_milli":10000,"net_fcfa":5000,"unit_kind":"piece"}]}

    def test_frozen_header_original_quantities_and_possible_rounding_must_match_the_request(self):
        from backups.services.recovery_sale_returns import decode_sale_return,RecoverySaleReturnInvalid
        decoded=decode_sale_return(self.source());self.assertEqual(decoded.snapshot["amount_fcfa"],5000)
        for case in ("total","obligation","quantity","original","piece","line","device","time","bool","physical"):
            with self.subTest(case=case):
                source=self.source();snapshot=source.payload["sale_return"];raw=source.payload["original_request"]
                if case=="total":snapshot["amount_fcfa"]=4999
                elif case=="obligation":snapshot["credit_fcfa"]=4999
                elif case=="quantity":snapshot["lines"][0]["quantity_milli"]=11000
                elif case=="original":snapshot["lines"][0]["original_amount_fcfa"]=4999
                elif case=="piece":snapshot["lines"][0]["quantity_milli"]=1500;raw["lines"][0]["quantity_milli"]=1500
                elif case=="line":raw["lines"][0]["sale_line_id"]=str(uuid.uuid4())
                elif case=="device":raw["installation_id"]=str(uuid.uuid4())
                elif case=="time":snapshot["occurred_at"]="2026-10-24T12:00:00Z"
                elif case=="bool":snapshot["amount_fcfa"]=True
                else:snapshot["vendable"]=True
                with self.assertRaises(RecoverySaleReturnInvalid):decode_sale_return(source)


class RecoverySaleReturnTests(TestCase,ReturnSources):
    authenticate=fixtures.RecoveryCustomerAllocationTests.authenticate
    generation=fixtures.RecoveryCustomerAllocationTests.generation
    record=fixtures.RecoveryCustomerAllocationTests.record
    device=fixtures.RecoveryCustomerAllocationTests.device
    review=fixtures.RecoveryCustomerAllocationTests.review
    payload=fixtures.RecoveryCustomerAllocationTests.payload
    url=fixtures.RecoveryCustomerAllocationTests.url
    capture=fixtures.RecoveryCustomerAllocationTests.capture
    preview=fixtures.RecoveryCustomerAllocationTests.preview
    apply=fixtures.RecoveryCustomerAllocationTests.apply

    def setUp(self):
        fixtures.RecoveryCustomerAllocationTests.setUp(self)
        event=Activity.objects.create(shop=self.a,actor=self.owner,action="opening_stock",target_id=self.product.id,reason="Stock de fixture")
        StockMovement.objects.create(product=self.product,actor=self.owner,operation=event,kind="opening",quantity_milli=50000,
            unit_cost_micros=100000000,occurred_at=datetime(2026,10,21,9,tzinfo=timezone.utc),reason="Ouverture de fixture")
        self.sale=self.prepare_sale()

    def prepare_sale(self,**changes):
        raw={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"occurred_at":"2026-10-23T09:00:00Z","reason":"Vente avant retour perdu",
            "customer_id":str(self.customer.id),"lines":[{"id":str(uuid.uuid4()),"product_id":str(self.product.id),
            "product_version":self.product.version,"quantity_milli":10000}],"payments":[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":5000}],**changes}
        if raw.get("mode")=="credit":raw.setdefault("confirmed_debt_fcfa",7000)
        response=self.client.post(f"/api/v1/shops/{self.a.id}/sales/",raw,format="json");self.assertEqual(response.status_code,201,response.content)
        return response.data

    def native_url(self,sale=None):
        return f"/api/v1/shops/{self.a.id}/sales/{(sale or self.sale)['id']}/returns/"

    def native_body(self,source):
        return {key:value for key,value in source.payload['original_request'].items() if key not in ('sale_id','installation_id')}

    def refund(self,returned_id):
        returned=SaleReturn.objects.get(id=returned_id)
        response=self.client.post(f"/api/v1/shops/{self.a.id}/sales/{returned.sale_id}/returns/{returned_id}/refunds/",
            {'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':'2026-10-25T12:00:00Z',
             'reason':'Remboursement réel postérieur','amount_fcfa':500,'method':'cash'},format='json')
        self.assertEqual(response.status_code,201,response.content)

    def test_lost_return_uses_billed_line_even_after_catalogue_change_without_stock_or_cash(self):
        source=self.source();capture=self.capture(source)
        self.product.sale_price_fcfa=9900;self.product.is_active=False;self.product.save()
        stock=StockMovement.objects.count();cash=list(CashMovement.objects.values_list("amount_fcfa",flat=True))
        response,command=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        receipt=response.data["business_receipt"]
        self.assertEqual((receipt["amount_fcfa"],receipt["credit_fcfa"],receipt["debt_reduced_fcfa"]),(5000,5000,0))
        self.assertEqual(receipt["actor_id"],str(source.origin_user_id))
        self.assertEqual((StockMovement.objects.count(),ReturnReceipt.objects.count()),(stock,0))
        self.assertEqual(list(CashMovement.objects.values_list("amount_fcfa",flat=True)),cash)
        self.assertEqual((SaleReturn.objects.count(),SaleReturnLine.objects.count()),(1,1))
        retry=self.client.post(self.url(f"operations/{capture.id}/integrate/"),command,format="json")
        self.assertEqual((retry.status_code,retry.data),(200,response.data))
        known=self.preview(capture);self.assertEqual(known["outcome"],"already_known")
        again,_=self.apply(capture,known);self.assertEqual((again.status_code,again.data["business_receipt"]),(201,receipt))

    def test_credit_return_reduces_original_sale_debt_then_creates_only_frozen_remaining_claim(self):
        sale=self.prepare_sale(mode="credit",payments=[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":2000}])
        source=self.source(sale);capture=self.capture(source)
        response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual((response.data["business_receipt"]["debt_reduced_fcfa"],response.data["business_receipt"]["credit_fcfa"]),(3000,2000))
        self.assertEqual(CustomerDebt.objects.get(sale_id=sale["id"]).remaining_fcfa,0)
        entry=CustomerLedgerEntry.objects.get(operation_id=source.operation_id)
        self.assertEqual((entry.delta_fcfa,entry.debt_delta_fcfa,entry.credit_delta_fcfa),(-5000,-3000,2000))
        self.assertEqual(sorted(CashMovement.objects.values_list("amount_fcfa",flat=True)),[2000,5000])

    def test_changed_financial_context_remains_captured_without_inventing_another_debt_credit_split(self):
        sale=self.prepare_sale(mode="credit",payments=[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":2000}])
        source=self.source(sale);capture=self.capture(source);old=self.preview(capture)
        debt=CustomerDebt.objects.get(sale_id=sale["id"]);debt.remaining_fcfa=2500;debt.save()
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        preview=self.preview(capture);self.assertFalse(preview["can_apply"])
        self.assertIn("return_financial_context_changed",{row["code"] for row in preview["issues"]})
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        self.assertFalse(SaleReturn.objects.exists());self.assertTrue(backups.RecoveryJournalOperation.objects.filter(id=capture.id).exists())

    def test_successive_recovered_returns_keep_33_34_33_and_do_not_exceed_quantity(self):
        sale=self.prepare_sale(global_discount_fcfa=1400,lines=[{"id":str(uuid.uuid4()),"product_id":str(self.product.id),
            "product_version":self.product.version,"quantity_milli":3000}],payments=[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":100}])
        sources=[]
        for amount in (33,34,33):
            source=self.source(sale);source.payload["original_request"]["lines"][0]["quantity_milli"]=1000
            source.payload["sale_return"].update(amount_fcfa=amount,credit_fcfa=amount)
            source.payload["sale_return"]["lines"][0].update(quantity_milli=1000,amount_fcfa=amount);sources.append(source)
        second=self.capture(sources[1]);self.assertFalse(self.preview(second)["can_apply"])
        for source in (sources[0],sources[1],sources[2]):
            capture=second if source is sources[1] else self.capture(source)
            response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(sum(SaleReturn.objects.values_list("amount_fcfa",flat=True)),100)
        self.assertEqual(sum(SaleReturnLine.objects.values_list("quantity_milli",flat=True)),3000)
        extra=self.source(sale);extra.payload["original_request"]["lines"][0]["quantity_milli"]=1000
        extra.payload["sale_return"].update(amount_fcfa=33,credit_fcfa=33);extra.payload["sale_return"]["lines"][0].update(quantity_milli=1000,amount_fcfa=33)
        self.assertFalse(self.preview(self.capture(extra))["can_apply"])

    def test_decision_failure_rolls_back_return_lines_debt_credit_ledger_and_origin(self):
        capture=self.capture(self.source());preview=self.preview(capture)
        with connection.cursor() as cursor:cursor.execute("CREATE TEMP TRIGGER fail_commercial_return BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected return failure'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(capture,preview)
        finally:
            with connection.cursor() as cursor:cursor.execute("DROP TRIGGER fail_commercial_return")
        self.assertEqual((SaleReturn.objects.count(),SaleReturnLine.objects.count(),backups.RecoveredOperationOrigin.objects.count()),(0,0,0))
        self.assertEqual(CashMovement.objects.get().amount_fcfa,5000)

    def test_native_return_after_later_refund_is_acknowledged_without_resetting_claim(self):
        from customers.services.return_credit_balance import remaining_return_credit
        source=self.source();source.origin_user_id=self.owner.id;source.origin_installation_id=self.live.installation_id
        source.payload['original_request']['installation_id']=str(self.live.installation_id)
        native=self.client.post(self.native_url(),self.native_body(source),format='json')
        self.assertEqual(native.status_code,201,native.content)
        for key in source.payload['sale_return']:
            if key in native.data:source.payload['sale_return'][key]=native.data[key]
        self.refund(native.data['id'])
        capture=self.capture(source);preview=self.preview(capture)
        self.assertEqual(preview['outcome'],'already_known');self.assertTrue(preview['origin_installation_verified'])
        response,_=self.apply(capture,preview);self.assertEqual((response.status_code,response.data['business_receipt']),(201,native.data))
        self.assertEqual(remaining_return_credit(SaleReturn.objects.get()),4500)
        self.assertEqual((SaleReturn.objects.count(),backups.RecoveredOperationOrigin.objects.count()),(1,0))

    def test_recovered_receipt_survives_next_generation_and_cancellation_without_new_effects(self):
        from customers.services.return_credit_balance import remaining_return_credit
        source=self.source();response,_=self.apply(self.capture(source));self.assertEqual(response.status_code,201,response.content)
        first=response.data['business_receipt'];self.refund(first['id'])
        cancel=self.client.post(f"/api/v1/shops/{self.a.id}/sales/{self.sale['id']}/cancellation/",
            {'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':'2026-10-25T13:00:00Z','reason':'Annulation postérieure'},format='json')
        self.assertEqual(cancel.status_code,201,cancel.content)
        self.current.current_slot=None;self.current.save();self.current=self.generation();self.registry=self.record(self.current)
        self.review(self.a,self.owner,'owner',confirmed=True)
        capture=self.capture(source);preview=self.preview(capture);self.assertEqual(preview['outcome'],'already_known')
        response,_=self.apply(capture,preview);self.assertEqual((response.status_code,response.data['business_receipt']),(201,first))
        operation=Activity.objects.get(id=source.operation_id)
        self.assertEqual(datetime.fromisoformat(operation.details['recovery']['source_snapshot']['created_at'].replace('Z','+00:00')),
            datetime.fromisoformat(source.payload['sale_return']['created_at']))
        self.assertNotEqual(datetime.fromisoformat(first['created_at'].replace('Z','+00:00')),
            datetime.fromisoformat(source.payload['sale_return']['created_at']))
        self.assertEqual(remaining_return_credit(SaleReturn.objects.get()),4500)
        self.assertEqual((SaleReturn.objects.count(),backups.RecoveredOperationOrigin.objects.count(),CashMovement.objects.count()),(1,1,2))

    def test_zero_value_and_anonymous_returns_preserve_quantities_without_customer_or_cash(self):
        for anonymous,price,discount in ((True,5000,0),(False,0,5000)):
            with self.subTest(anonymous=anonymous):
                sale=self.prepare_sale(customer_id=None if anonymous else str(self.customer.id),global_discount_fcfa=discount,
                    payments=[{'id':str(uuid.uuid4()),'method':'cash','amount_fcfa':price}] if price else [])
                ledger=CustomerLedgerEntry.objects.count();cash=CashMovement.objects.count();customers=self.customer.__class__.objects.count()
                source=self.source(sale);response,_=self.apply(self.capture(source));self.assertEqual(response.status_code,201,response.content)
                self.assertEqual(response.data['business_receipt']['amount_fcfa'],price)
                self.assertEqual(CustomerLedgerEntry.objects.count(),ledger+(not anonymous))
                self.assertEqual((CashMovement.objects.count(),self.customer.__class__.objects.count(),ReturnReceipt.objects.count()),(cash,customers,0))

    def test_missing_or_foreign_sale_and_line_dependencies_and_identity_remain_blocked(self):
        from identity.models import Installation
        for case,code in (('sale','missing_sale'),('line','missing_sale_line'),('customer','return_customer_conflict'),('device','origin_identity_conflict')):
            with self.subTest(case=case):
                source=self.source()
                if case=='sale':
                    source.payload['sale_return']['sale_id']=str(uuid.uuid4());source.payload['original_request']['sale_id']=source.payload['sale_return']['sale_id']
                elif case=='line':
                    identifier=str(uuid.uuid4());source.payload['sale_return']['lines'][0]['sale_line_id']=identifier
                    source.payload['original_request']['lines'][0]['sale_line_id']=identifier
                elif case=='customer':source.payload['sale_return']['customer_id']=str(uuid.uuid4())
                else:
                    source.origin_installation_id=self.live.installation_id
                    source.payload['original_request']['installation_id']=str(self.live.installation_id)
                    self.assertTrue(Installation.objects.filter(id=source.origin_installation_id).exists())
                capture=self.capture(source,[uuid.uuid4()]);preview=self.preview(capture)
                self.assertFalse(preview['can_apply']);self.assertIn(code,{row['code'] for row in preview['issues']})
                self.assertIn('missing_dependencies',{row['code'] for row in preview['issues']})
                if case=='sale':self.assertIsNone(preview['current_state'])
                self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        self.assertFalse(SaleReturn.objects.exists())

    def test_actual_foreign_sale_and_line_are_not_loaded_into_preview(self):
        from sales.models import Sale
        foreign=self.prepare_sale();Sale.objects.filter(id=foreign['id']).update(shop=self.b)
        source=self.source(foreign);preview=self.preview(self.capture(source))
        self.assertFalse(preview['can_apply']);self.assertIsNone(preview['current_state'])
        self.assertIn('missing_sale',{row['code'] for row in preview['issues']})
        source=self.source()
        for row in (source.payload['original_request']['lines'][0],source.payload['sale_return']['lines'][0]):
            row['sale_line_id']=foreign['lines'][0]['id']
        preview=self.preview(self.capture(source));self.assertFalse(preview['can_apply'])
        self.assertIsNone(preview['current_state']['lines'][0]['original_amount_fcfa'])
        self.assertIn('missing_sale_line',{row['code'] for row in preview['issues']})

    def test_new_return_of_cancelled_sale_remains_captured_without_second_return(self):
        source=self.source();capture=self.capture(source)
        cancel=self.client.post(f"/api/v1/shops/{self.a.id}/sales/{self.sale['id']}/cancellation/",
            {'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':'2026-10-25T13:00:00Z','reason':'Annulation contrôlée'},format='json')
        self.assertEqual(cancel.status_code,201,cancel.content)
        preview=self.preview(capture);self.assertFalse(preview['can_apply'])
        self.assertIn('sale_cancelled',{row['code'] for row in preview['issues']})
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        self.assertEqual((SaleReturn.objects.count(),CashMovement.objects.count(),ReturnReceipt.objects.count()),(1,1,0))

    def test_known_return_with_corrupt_immutable_model_header_is_not_acknowledged(self):
        source=self.source();capture=self.capture(source)
        response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        SaleReturn.objects.filter(id=source.payload['sale_return']['id']).update(amount_fcfa=4999,credit_fcfa=4999)
        preview=self.preview(capture);self.assertFalse(preview['can_apply'])
        self.assertIn('operation_conflict',{row['code'] for row in preview['issues']})
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)

    def test_missing_parent_sale_is_recovered_before_its_return_without_second_sale_stock_or_cash(self):
        from backups.tests.test_recovery_sale_snapshot import RecoverySaleSnapshotTests
        from sales.models import Sale
        from customers.services.return_credit_balance import remaining_return_credit
        sale=RecoverySaleSnapshotTests().source();sale.operation_type='sale'
        for payload in (sale.payload['original_request'],sale.payload['sale']):
            payload['customer_id']=str(self.customer.id)
            payload['lines'][0]['product_id']=str(self.product.id)
        source=self.source(sale.payload['sale']);capture=self.capture(source,[sale.operation_id]);old=self.preview(capture)
        self.assertFalse(old['can_apply']);self.assertIn('missing_sale',{row['code'] for row in old['issues']})
        response,_=self.apply(self.capture(sale));self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        returned=SaleReturn.objects.get();self.assertEqual((returned.amount_fcfa,remaining_return_credit(returned)),(500,500))
        self.assertEqual((Sale.objects.count(),StockMovement.objects.count(),CashMovement.objects.count(),ReturnReceipt.objects.count()),(2,3,2,0))
        self.assertEqual(sum(CashMovement.objects.values_list('amount_fcfa',flat=True)),5500)
        self.refund(returned.id);self.assertEqual(remaining_return_credit(returned),0)
        self.assertEqual(sum(CashMovement.objects.values_list('amount_fcfa',flat=True)),5000)
        self.assertEqual(self.preview(capture)['outcome'],'already_known')

    def test_current_rights_and_native_bypass_are_checked_before_integration(self):
        from identity.models import User,Membership
        source=self.source();capture=self.capture(source)
        seller=User.objects.create_user(username='commercial-return-seller');member=Membership.objects.create(shop=self.a,user=seller)
        self.review(self.a,seller,'seller',membership=member,confirmed=True);api=self.authenticate(seller)
        self.assertEqual(api.generic('POST',self.url(f'operations/{capture.id}/integrate/'),b'invalid',content_type='application/json').status_code,403)
        self.assertEqual(self.client.post(self.native_url(),self.native_body(source),format='json').status_code,409)
        self.assertEqual(self.client.get(self.url(f'operations/{capture.id}/integration-preview/',shop=self.b)).status_code,404)
        preview=self.preview(capture)
        review=backups.RestoreAccessReview.objects.get(generation=self.current,shop=self.a,user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=1,version=2,state='refused',reason='Qualité actuelle retirée')
        self.assertEqual(self.apply(capture,preview)[0].status_code,404);self.assertFalse(SaleReturn.objects.exists())

    def test_corrupt_origin_refuses_list_and_detail_without_changing_return(self):
        source=self.source();capture=self.capture(source);response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        backups.RecoveryJournalOperation.objects.filter(id=capture.id).update(shop=self.b,origin_user_id=uuid.uuid4())
        for language in ('fr','en'):
            for suffix in ('',source.payload['sale_return']['id']+'/'):
                with self.subTest(language=language,suffix=suffix):
                    response=self.client.get(self.native_url()+suffix,HTTP_ACCEPT_LANGUAGE=language)
                    self.assertEqual(response.status_code,409,response.content);self.assertEqual(response.data['code'],'origin_reference_conflict')
        self.assertEqual((SaleReturn.objects.get().credit_fcfa,CashMovement.objects.get().amount_fcfa),(5000,5000))

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
        source=self.source();capture=self.capture(source)
        preview=self.client.get(self.url(f'operations/{capture.id}/integration-preview/'));self.assertEqual(preview.status_code,200,preview.content);check(preview,'/integration-preview/')
        response,_=self.apply(capture,preview.data);self.assertEqual(response.status_code,201,response.content);check(response,'/integrate/','post')
        for suffix,ending in (('','/sales/{sale_id}/returns/'),(source.payload['sale_return']['id']+'/','/sales/{sale_id}/returns/{id}/')):
            history=self.client.get(self.native_url()+suffix);self.assertEqual(history.status_code,200,history.content);check(history,ending)
            row=history.data['results'][0] if not suffix else history.data
            self.assertEqual(row['actor_id'],str(source.origin_user_id))
        source=self.source();source.payload['sale_return']['amount_fcfa']=1;capture=self.capture(source)
        for language,detail in (('en','Recovery commercial return snapshot is inconsistent.'),('fr','Les données figées du retour commercial récupéré sont incohérentes.')):
            with self.subTest(language=language):
                response=self.client.get(self.url(f'operations/{capture.id}/integration-preview/'),HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual((response.status_code,str(response.data['detail'])),(400,detail))
