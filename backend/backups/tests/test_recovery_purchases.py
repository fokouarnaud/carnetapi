"""Reviewed purchase obligations keep frozen lines apart from goods and money."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import uuid

from django.db import connection,IntegrityError
from django.test import TestCase,SimpleTestCase
from django.test.utils import CaptureQueriesContext

from backups import models as backups
from backups.tests import test_recovery_supplier_dependencies as fixtures
from catalogue.models import Product,ProductPack
from cash.models import CashMovement
from common.models import Activity
from identity.models import DeviceSession,Membership,User
from purchasing.models import PurchaseDocument,PurchaseLine,PurchaseReceipt,Supplier,SupplierDebt,SupplierLedgerEntry
from stock.models import StockMovement


class PurchaseSources:
    def purchase_source(self,kind='invoice',order_id=None):
        identifier,operation,actor,device=[uuid.uuid4() for _ in range(4)]
        occurred=datetime(2026,10,24,12,tzinfo=timezone.utc);created=occurred+timedelta(seconds=3)
        line={'id':str(uuid.uuid4()),'product_id':str(self.product.id),'pack_id':str(self.pack.id),
            'product_name':'Produit au départ','unit_label':'Carton de 24','quantity_milli':10000,
            'factor_milli':24000,'base_quantity_milli':240000,'unit_price_fcfa':1000,'total_fcfa':10000,
            'received_quantity_milli':0,'awaiting_quantity_milli':10000}
        raw={'id':str(identifier),'operation_id':str(operation),'supplier_id':str(self.supplier_id),'installation_id':str(device),
            'occurred_at':occurred.isoformat(),'reason':'Document fournisseur réellement validé','external_reference':'ACHAT-T1',
            'order_id':str(order_id) if order_id else None,
            'lines':[{key:line[key] for key in ('id','product_id','pack_id','quantity_milli','unit_price_fcfa')}]}
        debt={'id':str(uuid.uuid4()),'document_id':str(identifier),'amount_fcfa':10000,'remaining_fcfa':10000,
            'occurred_at':occurred.isoformat(),'created_at':created.isoformat()} if kind=='invoice' else None
        snapshot={'id':str(identifier),'supplier_id':str(self.supplier_id),'kind':kind,'order_id':raw['order_id'],
            'occurred_at':occurred.isoformat(),'created_at':created.isoformat(),'external_reference':'ACHAT-T1',
            'total_fcfa':10000,'lines':[line],'debt':debt}
        return SimpleNamespace(operation_type='purchase_'+kind,operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=occurred,payload={'schema_version':1,'original_request':raw,'purchase':snapshot})


class RecoveryPurchaseSnapshotTests(SimpleTestCase,PurchaseSources):
    def setUp(self):
        self.supplier_id=uuid.uuid4();self.product=SimpleNamespace(id=uuid.uuid4());self.pack=SimpleNamespace(id=uuid.uuid4())

    def test_original_lines_totals_and_separate_debt_must_match_exactly(self):
        from backups.services.recovery_purchase_snapshots import decode_purchase,RecoveryPurchaseInvalid
        for kind in ('order','invoice'):
            source=self.purchase_source(kind);self.assertEqual(decode_purchase(source).snapshot['total_fcfa'],10000)
        for case in ('kind','total','factor','base','price','parent','line','received','awaiting','debt','balance','date','extra','bool'):
            with self.subTest(case=case):
                source=self.purchase_source();snapshot=source.payload['purchase'];line=snapshot['lines'][0]
                if case=='kind':snapshot['kind']='order'
                elif case=='total':snapshot['total_fcfa']=9999
                elif case=='factor':line['factor_milli']=23999
                elif case=='base':line['base_quantity_milli']=239000
                elif case=='price':line['unit_price_fcfa']=999
                elif case=='parent':source.payload['original_request']['supplier_id']=str(uuid.uuid4())
                elif case=='line':line['id']=str(uuid.uuid4())
                elif case=='received':line['received_quantity_milli']=1000
                elif case=='awaiting':line['awaiting_quantity_milli']=9000
                elif case=='debt':snapshot['debt']=None
                elif case=='balance':snapshot['debt']['remaining_fcfa']=9000
                elif case=='date':snapshot['created_at']='2026-10-24T11:00:00Z'
                elif case=='extra':snapshot['stock_milli']=1
                else:line['unit_price_fcfa']=True
                with self.assertRaises(RecoveryPurchaseInvalid):decode_purchase(source)

    def test_zero_invoice_and_fractional_half_up_total_are_creation_facts(self):
        from backups.services.recovery_purchase_snapshots import decode_purchase,RecoveryPurchaseInvalid
        source=self.purchase_source();snapshot=source.payload['purchase'];line=snapshot['lines'][0];raw=source.payload['original_request']['lines'][0]
        line.update(pack_id=None,quantity_milli=500,factor_milli=1000,base_quantity_milli=500,unit_price_fcfa=249,total_fcfa=125,awaiting_quantity_milli=500)
        raw.update(pack_id=None,quantity_milli=500,unit_price_fcfa=249);snapshot['total_fcfa']=125
        snapshot['debt'].update(amount_fcfa=125,remaining_fcfa=125)
        self.assertEqual(decode_purchase(source).snapshot['total_fcfa'],125)
        line.update(unit_price_fcfa=0,total_fcfa=0);raw['unit_price_fcfa']=0;snapshot['total_fcfa']=0
        snapshot['debt'].update(amount_fcfa=0,remaining_fcfa=0)
        self.assertEqual(decode_purchase(source).snapshot['debt']['amount_fcfa'],0)
        source=self.purchase_source('order');source.payload['purchase']['debt']=deepcopy(self.purchase_source().payload['purchase']['debt'])
        with self.assertRaises(RecoveryPurchaseInvalid):decode_purchase(source)


class RecoveryPurchaseTests(TestCase,PurchaseSources):
    authenticate=fixtures.RecoverySupplierDependencyTests.authenticate
    generation=fixtures.RecoverySupplierDependencyTests.generation
    record=fixtures.RecoverySupplierDependencyTests.record
    device=fixtures.RecoverySupplierDependencyTests.device
    review=fixtures.RecoverySupplierDependencyTests.review
    payload=fixtures.RecoverySupplierDependencyTests.payload
    url=fixtures.RecoverySupplierDependencyTests.url
    capture=fixtures.RecoverySupplierDependencyTests.capture
    preview=fixtures.RecoverySupplierDependencyTests.preview
    apply=fixtures.RecoverySupplierDependencyTests.apply
    native_url=fixtures.RecoverySupplierDependencyTests.native_url
    native_body=fixtures.RecoverySupplierDependencyTests.native_body
    native_supplier=fixtures.RecoverySupplierDependencyTests.native_supplier
    supplier_source=fixtures.RecoverySupplierDependencyTests.supplier_source

    def setUp(self):
        fixtures.RecoverySupplierDependencyTests.setUp(self)
        self.supplier_id=self.native_supplier()
        self.product=Product.objects.create(shop=self.a,name='Catalogue courant',unit_kind='piece',unit_label='pièce',sale_price_fcfa=5000)
        self.pack=ProductPack.objects.create(product=self.product,name='Carton courant',base_quantity_milli=12000,sale_price_fcfa=9999)

    def purchase_url(self,kind='invoice',identifier=None,suffix=''):
        return self.native_url(self.supplier_id,kind+'s/'+(str(identifier)+'/' if identifier else '')+suffix)

    def native_purchase(self,kind='invoice',order_id=None):
        source=self.purchase_source(kind,order_id);session=DeviceSession.objects.get(installation__user=self.owner)
        source.origin_user_id=self.owner.id;source.origin_installation_id=session.installation_id
        source.payload['original_request']['installation_id']=str(session.installation_id)
        response=self.client.post(self.purchase_url(kind),self.native_body(source),format='json')
        self.assertEqual(response.status_code,201,response.content)
        source.payload['purchase']={**response.data,'supplier_id':str(self.supplier_id)}
        return source,response.data

    def test_order_then_invoice_recover_frozen_lines_and_debt_without_goods_or_money(self):
        order=self.purchase_source('order');invoice=self.purchase_source(order_id=order.payload['purchase']['id'])
        first=self.capture(order);second=self.capture(invoice,[order.operation_id]);old=self.preview(second)
        self.assertFalse(old['can_apply']);self.assertIn('missing_order',{row['code'] for row in old['issues']})
        for capture,expected_debt in ((first,0),(second,10000)):
            current=self.preview(capture);self.assertEqual(current['new_supplier_debt_fcfa'],expected_debt)
            self.assertEqual((current['stocks'],current['cash_delta_by_method_fcfa']),([],{ }))
            if capture==second:self.assertEqual(self.apply(capture,old)[0].status_code,409)
            response,command=self.apply(capture,current);self.assertEqual(response.status_code,201,response.content)
            self.assertEqual(self.client.post(self.url(f'operations/{capture.id}/integrate/'),command,format='json').data,response.data)
        self.assertEqual((PurchaseDocument.objects.count(),PurchaseLine.objects.count(),SupplierDebt.objects.count(),SupplierLedgerEntry.objects.count()),(2,2,1,1))
        debt=SupplierDebt.objects.get();self.assertEqual(str(debt.id),invoice.payload['purchase']['debt']['id'])
        self.assertEqual((debt.amount_fcfa,debt.remaining_fcfa),(10000,10000))
        self.assertEqual(list(PurchaseLine.objects.values_list('factor_milli','base_quantity_milli','unit_price_fcfa','product_name','unit_label')),
            [(24000,240000,1000,'Produit au départ','Carton de 24')]*2)
        self.assertEqual((CashMovement.objects.count(),StockMovement.objects.count(),PurchaseReceipt.objects.count()),(0,0,0))
        self.assertEqual((backups.RecoveredOperationOrigin.objects.count(),backups.RecoveryIntegrationDecision.objects.count()),(2,2))

    def test_archived_catalogue_preserves_source_values_and_future_dates_block(self):
        source=self.purchase_source();capture=self.capture(source)
        Product.objects.filter(id=self.product.id).update(is_active=False,name='Nom archivé')
        ProductPack.objects.filter(id=self.pack.id).update(is_active=False,base_quantity_milli=6000)
        response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(PurchaseLine.objects.get().factor_milli,24000)
        source=self.purchase_source();future=datetime.now(timezone.utc)+timedelta(days=1)
        source.occurred_at=future
        for obj in (source.payload['original_request'],source.payload['purchase'],source.payload['purchase']['debt']):obj['occurred_at']=future.isoformat()
        for obj in (source.payload['purchase'],source.payload['purchase']['debt']):obj['created_at']=future.isoformat()
        current=self.preview(self.capture(source));self.assertFalse(current['can_apply'])
        self.assertIn('future_effective_date',{row['code'] for row in current['issues']})

    def test_missing_supplier_product_pack_order_and_dependencies_are_scoped(self):
        foreign_supplier=Supplier.objects.create(shop=self.b,name='Fournisseur confidentiel B')
        foreign_product=Product.objects.create(shop=self.b,name='Produit confidentiel B',unit_kind='piece',unit_label='pièce',sale_price_fcfa=1)
        foreign_pack=ProductPack.objects.create(product=foreign_product,name='Format confidentiel B',base_quantity_milli=1000,sale_price_fcfa=1)
        for case in ('supplier','product','pack','order'):
            with self.subTest(case=case):
                source=self.purchase_source();snapshot=source.payload['purchase'];raw=source.payload['original_request']
                if case=='supplier':snapshot['supplier_id']=raw['supplier_id']=str(foreign_supplier.id)
                elif case in ('product','pack'):
                    key=case+'_id';identifier=foreign_product.id if case=='product' else foreign_pack.id
                    snapshot['lines'][0][key]=raw['lines'][0][key]=str(identifier)
                else:snapshot['order_id']=raw['order_id']=str(uuid.uuid4())
                capture=self.capture(source,[uuid.uuid4()]);current=self.preview(capture)
                self.assertFalse(current['can_apply']);self.assertIn('missing_'+case,{row['code'] for row in current['issues']})
                self.assertIn('missing_dependencies',{row['code'] for row in current['issues']})
                self.assertNotIn('confidentiel',str(current));self.assertEqual(self.apply(capture,current)[0].status_code,409)
        self.assertFalse(PurchaseDocument.objects.exists())

    def test_unit_precision_uuid_and_ledger_collisions_block_before_any_write(self):
        source=self.purchase_source();line=source.payload['purchase']['lines'][0];raw=source.payload['original_request']['lines'][0]
        line.update(pack_id=None,quantity_milli=500,factor_milli=1000,base_quantity_milli=500,total_fcfa=500,awaiting_quantity_milli=500)
        raw.update(pack_id=None,quantity_milli=500);source.payload['purchase']['total_fcfa']=500
        source.payload['purchase']['debt'].update(amount_fcfa=500,remaining_fcfa=500)
        current=self.preview(self.capture(source));self.assertFalse(current['can_apply'])
        self.assertIn('product_unit_conflict',{row['code'] for row in current['issues']})
        native,receipt=self.native_purchase()
        for case in ('document','line','debt','ledger'):
            with self.subTest(case=case):
                source=self.purchase_source();snapshot=source.payload['purchase'];raw=source.payload['original_request']
                if case=='document':snapshot['id']=raw['id']=receipt['id'];snapshot['debt']['document_id']=receipt['id']
                elif case=='line':snapshot['lines'][0]['id']=raw['lines'][0]['id']=receipt['lines'][0]['id']
                elif case=='debt':snapshot['debt']['id']=receipt['debt']['id']
                else:
                    existing=SupplierLedgerEntry.objects.get(operation_id=native.operation_id)
                    SupplierLedgerEntry.objects.filter(id=existing.id).update(id=uuid.uuid5(uuid.UUID(snapshot['id']),'supplier-ledger:invoice'))
                current=self.preview(self.capture(source));self.assertFalse(current['can_apply'])
                self.assertIn('entity_uuid_conflict',{row['code'] for row in current['issues']})
        self.assertEqual(PurchaseDocument.objects.count(),1)

    def test_late_decision_failure_rolls_back_document_lines_debt_origin_and_ledger(self):
        for kind in ('order','invoice'):
            with self.subTest(kind=kind):
                source=self.purchase_source(kind);capture=self.capture(source)
                with connection.cursor() as cursor:cursor.execute("CREATE TEMP TRIGGER fail_purchase_review BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected purchase review failure'); END")
                try:
                    with self.assertRaises(IntegrityError):self.apply(capture)
                finally:
                    with connection.cursor() as cursor:cursor.execute('DROP TRIGGER fail_purchase_review')
                for model in (PurchaseDocument,PurchaseLine,SupplierDebt,SupplierLedgerEntry,backups.RecoveredOperationOrigin):self.assertFalse(model.objects.exists())
                self.assertFalse(Activity.objects.filter(id=source.operation_id).exists())

    def test_native_and_recovered_creation_receipts_remain_known_after_receipt_and_payment(self):
        native,first_native=self.native_purchase();recovered=self.purchase_source();capture=self.capture(recovered)
        response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content);first_recovered=response.data['business_receipt']
        for source,first in ((native,first_native),(recovered,first_recovered)):
            received=self.client.post(self.purchase_url(identifier=first['id'],suffix='receipts/'),
                {'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':'2026-10-25T12:00:00Z','reason':'Réception physique postérieure',
                 'lines':[{'line_id':first['lines'][0]['id'],'quantity_milli':6000}]},format='json')
            self.assertEqual(received.status_code,201,received.content)
            payment=self.client.post(self.native_url(self.supplier_id,'payments/'),
                {'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':'2026-10-25T12:00:00Z','reason':'Paiement réel distinct',
                 'parts':[{'id':str(uuid.uuid4()),'method':'cash','amount_fcfa':3000}]},format='json')
            self.assertEqual(payment.status_code,201,payment.content)
            allocated=self.client.post(self.native_url(self.supplier_id,f"payments/{payment.data['id']}/allocate/"),
                {'operation_id':str(uuid.uuid4()),'reason':'Affectation après création','allocations':[{'debt_id':first['debt']['id'],'amount_fcfa':3000}]},format='json')
            self.assertEqual(allocated.status_code,201,allocated.content)
            current=self.preview(self.capture(source));self.assertEqual(current['outcome'],'already_known',current)
            response,_=self.apply(self.capture(source),current);self.assertEqual(response.data['business_receipt'],first)
        self.assertEqual(sorted(SupplierDebt.objects.values_list('remaining_fcfa',flat=True)),[7000,7000])
        self.assertEqual((StockMovement.objects.count(),CashMovement.objects.count()),(2,2))

    def test_known_document_checks_immutable_rows_debt_and_ledger(self):
        for case in ('line','debt','ledger','document'):
            with self.subTest(case=case):
                source,receipt=self.native_purchase();capture=self.capture(source)
                if case=='line':PurchaseLine.objects.filter(id=receipt['lines'][0]['id']).update(product_name='Altéré')
                elif case=='debt':SupplierDebt.objects.filter(id=receipt['debt']['id']).update(document=None)
                elif case=='ledger':SupplierLedgerEntry.objects.filter(operation_id=source.operation_id).update(source_id=uuid.uuid4())
                else:PurchaseDocument.objects.filter(id=receipt['id']).update(external_reference='Altérée')
                current=self.preview(capture);self.assertFalse(current['can_apply']);self.assertIn('operation_conflict',{row['code'] for row in current['issues']})

    def test_permissions_captured_uuid_and_foreign_operation_origin_are_checked(self):
        source=self.purchase_source();capture=self.capture(source)
        self.assertEqual(self.client.post(self.purchase_url(),self.native_body(source),format='json').status_code,409)
        seller=User.objects.create_user(username='purchase-recovery-seller');member=Membership.objects.create(shop=self.a,user=seller)
        self.review(self.a,seller,'seller',membership=member,confirmed=True);api=self.authenticate(seller)
        self.assertEqual(api.generic('POST',self.url(f'operations/{capture.id}/integrate/'),b'invalid',content_type='application/json').status_code,403)
        self.assertEqual(self.client.get(self.url(f'operations/{capture.id}/integration-preview/',shop=self.b)).status_code,404)
        source,receipt=self.native_purchase();Activity.objects.filter(id=source.operation_id).update(shop=self.b)
        current=self.preview(self.capture(source));self.assertFalse(current['can_apply']);self.assertIn('operation_conflict',{row['code'] for row in current['issues']})

    def test_source_times_survive_in_metadata_without_claiming_old_persistence_and_next_generation_is_known(self):
        source=self.purchase_source();capture=self.capture(source);response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        first=response.data['business_receipt'];operation=Activity.objects.get(id=source.operation_id)
        self.assertEqual(operation.details['recovery']['source_snapshot']['created_at'],source.payload['purchase']['created_at'].replace('+00:00','Z'))
        self.assertNotEqual(first['created_at'],operation.details['recovery']['source_snapshot']['created_at'])
        self.current.current_slot=None;self.current.save();self.current=self.generation();self.registry=self.record(self.current)
        self.review(self.a,self.owner,'owner',confirmed=True)
        current=self.preview(self.capture(source));self.assertEqual(current['outcome'],'already_known',current)
        response,_=self.apply(self.capture(source),current);self.assertEqual(response.data['business_receipt'],first)
        self.assertEqual((PurchaseDocument.objects.count(),SupplierDebt.objects.count(),SupplierLedgerEntry.objects.count()),(1,1,1))

    def test_zero_and_fractional_invoice_amounts_recover_without_invented_goods_or_money(self):
        Product.objects.filter(id=self.product.id).update(unit_kind='weight')
        for price,total in ((0,0),(249,125)):
            with self.subTest(price=price):
                source=self.purchase_source();snapshot=source.payload['purchase'];line=snapshot['lines'][0]
                line.update(pack_id=None,quantity_milli=500,factor_milli=1000,base_quantity_milli=500,
                    unit_price_fcfa=price,total_fcfa=total,awaiting_quantity_milli=500)
                source.payload['original_request']['lines'][0].update(pack_id=None,quantity_milli=500,unit_price_fcfa=price)
                snapshot['total_fcfa']=total;snapshot['debt'].update(amount_fcfa=total,remaining_fcfa=total)
                capture=self.capture(source);preview=self.preview(capture);self.assertEqual(preview['new_supplier_debt_fcfa'],total)
                response,_=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)
                self.assertEqual(response.data['business_receipt']['debt']['amount_fcfa'],total)
                current=self.preview(capture);self.assertEqual(current['outcome'],'already_known',current)
        self.assertEqual(sorted(SupplierDebt.objects.values_list('remaining_fcfa',flat=True)),[0,125])
        self.assertEqual(sorted(SupplierLedgerEntry.objects.values_list('debt_delta_fcfa',flat=True)),[0,125])
        self.assertEqual((StockMovement.objects.count(),CashMovement.objects.count()),(0,0))

    def test_foreign_native_or_captured_origin_blocks_public_reads_before_activity_details(self):
        source=self.purchase_source();capture=self.capture(source);response,_=self.apply(capture)
        self.assertEqual(response.status_code,201,response.content)
        for case in ('capture','activity'):
            with self.subTest(case=case):
                model,identifier=(backups.RecoveryJournalOperation,capture.id) if case=='capture' else (Activity,source.operation_id)
                model.objects.filter(id=identifier).update(shop=self.b)
                for path in (self.purchase_url(),self.purchase_url(identifier=source.payload['purchase']['id'])):
                    with CaptureQueriesContext(connection) as queries:current=self.client.get(path)
                    self.assertEqual((current.status_code,current.data['code']),(409,'origin_reference_conflict'),current.content)
                    self.assertFalse(any('"core_activity"."details"' in row['sql'] for row in queries.captured_queries))
                model.objects.filter(id=identifier).update(shop=self.a)

    def test_foreign_debt_link_never_exposes_another_shops_balance_in_purchase_receipt(self):
        _source,receipt=self.native_purchase()
        SupplierDebt.objects.filter(id=receipt['debt']['id']).update(shop=self.b)
        for path in (self.purchase_url(),self.purchase_url(identifier=receipt['id'])):
            current=self.client.get(path)
            self.assertEqual((current.status_code,current.data.get('code')),(409,'origin_reference_conflict'),current.content)

    def test_foreign_product_or_pack_link_and_parent_order_block_before_frozen_lines(self):
        _source,receipt=self.native_purchase();line=PurchaseLine.objects.get(id=receipt['lines'][0]['id'])
        foreign=Product.objects.create(shop=self.b,name='Secret produit B',unit_kind='piece',unit_label='pièce',sale_price_fcfa=1)
        foreign_pack=ProductPack.objects.create(product=foreign,name='Secret format B',base_quantity_milli=1000,sale_price_fcfa=1)
        for case in ('product','pack'):
            with self.subTest(case=case):
                PurchaseLine.objects.filter(id=line.id).update(**{case+'_id':foreign.id if case=='product' else foreign_pack.id})
                current=self.client.get(self.purchase_url(identifier=receipt['id']))
                self.assertEqual((current.status_code,current.data.get('code')),(409,'origin_reference_conflict'),current.content)
                PurchaseLine.objects.filter(id=line.id).update(product_id=self.product.id,pack_id=self.pack.id)
        _order,order_receipt=self.native_purchase('order')
        PurchaseDocument.objects.filter(id=receipt['id']).update(order_id=order_receipt['id'])
        PurchaseDocument.objects.filter(id=order_receipt['id']).update(shop=self.b)
        current=self.client.get(self.purchase_url(identifier=receipt['id']))
        self.assertEqual((current.status_code,current.data.get('code')),(409,'origin_reference_conflict'),current.content)
