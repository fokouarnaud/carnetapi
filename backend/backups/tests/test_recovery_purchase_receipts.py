"""Physical purchase arrivals survive recovery without a second payable or cash effect."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

from django.test import TestCase, SimpleTestCase
from django.db import connection,IntegrityError

from backups.tests import test_recovery_purchases as fixtures
from catalogue.models import Product,ProductPack
from cash.models import CashMovement
from common.models import Activity
from identity.models import DeviceSession
from purchasing.models import PurchaseLine, PurchaseReceipt, PurchaseReceiptLine, SupplierDebt, SupplierLedgerEntry,PurchaseChargeAllocation
from stock.models import StockMovement


class RecoveryPurchaseReceiptTests(TestCase):
    def setUp(self):
        fixtures.RecoveryPurchaseTests.setUp(self)
        self.pack.base_quantity_milli=24000;self.pack.save(update_fields=['base_quantity_milli','updated_at'])
    authenticate=fixtures.RecoveryPurchaseTests.authenticate
    generation=fixtures.RecoveryPurchaseTests.generation
    record=fixtures.RecoveryPurchaseTests.record
    device=fixtures.RecoveryPurchaseTests.device
    review=fixtures.RecoveryPurchaseTests.review
    payload=fixtures.RecoveryPurchaseTests.payload
    url=fixtures.RecoveryPurchaseTests.url
    capture=fixtures.RecoveryPurchaseTests.capture
    preview=fixtures.RecoveryPurchaseTests.preview
    apply=fixtures.RecoveryPurchaseTests.apply
    native_url=fixtures.RecoveryPurchaseTests.native_url
    native_body=fixtures.RecoveryPurchaseTests.native_body
    native_supplier=fixtures.RecoveryPurchaseTests.native_supplier
    supplier_source=fixtures.RecoveryPurchaseTests.supplier_source
    purchase_source=fixtures.RecoveryPurchaseTests.purchase_source
    purchase_url=fixtures.RecoveryPurchaseTests.purchase_url
    native_purchase=fixtures.RecoveryPurchaseTests.native_purchase

    def receipt_source(self, invoice, quantity=6000):
        identifier=uuid.uuid4();operation=uuid.uuid4();device=uuid.uuid4();actor=uuid.uuid4()
        line=invoice['lines'][0];before=0 if quantity==6000 else 144000
        raw={'id':str(identifier),'operation_id':str(operation),'document_id':invoice['id'],
            'installation_id':str(device),'occurred_at':'2026-10-25T12:00:00Z','reason':'Arrivée physique confirmée',
            'lines':[{'line_id':line['id'],'quantity_milli':quantity}]}
        frozen={'id':str(identifier),'document_id':invoice['id'],'occurred_at':raw['occurred_at'],
            'created_at':'2026-10-25T12:00:03Z','lines':[{
                'id':str(uuid.uuid5(identifier,'purchase-receipt-line:'+line['id'])),
                'movement_id':str(uuid.uuid5(identifier,'purchase-stock:'+line['id'])),
                'line_id':line['id'],'product_id':line['product_id'],'unit_kind':'piece',
                'quantity_milli':quantity,'base_quantity_milli':quantity*24,
                'cost_basis':{'invoice_total_fcfa':10000,'invoice_base_quantity_milli':240000,
                    'received_before_base_milli':before,'total_cost_nanos_fcfa':str(quantity*1000000000),
                    'charge_allocations':[]}}]}
        return SimpleNamespace(operation_type='purchase_receipt',operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=datetime(2026,10,25,12,tzinfo=timezone.utc),
            payload={'schema_version':1,'original_request':raw,'receipt':frozen})

    def test_native_receipt_keeps_original_row_ids_and_exact_cost_basis_for_later_recovery(self):
        _,invoice=self.native_purchase()
        source=self.receipt_source(invoice);raw=self.native_body(source);raw.pop('document_id')
        endpoint=self.purchase_url('invoice',invoice['id'],'receipts/')
        response=self.client.post(endpoint,raw,format='json');self.assertEqual(response.status_code,201,response.content)
        row=response.data['lines'][0]
        self.assertEqual(row['id'],source.payload['receipt']['lines'][0]['id'])
        self.assertEqual(row['movement_id'],source.payload['receipt']['lines'][0]['movement_id'])
        self.assertEqual(row['cost_basis']['total_cost_nanos_fcfa'],'6000000000000')
        self.assertEqual(row['cost_basis']['received_before_base_milli'],0)
        self.assertEqual(self.client.post(endpoint,raw,format='json').data,response.data)
        self.assertEqual((StockMovement.objects.count(),PurchaseReceiptLine.objects.count()),(1,1))
        self.assertEqual((SupplierDebt.objects.get().remaining_fcfa,CashMovement.objects.count()),(10000,0))

    def test_recovery_6_then_4_keeps_factor_stock_ids_and_no_second_debt_or_money(self):
        _,invoice=self.native_purchase()
        first=self.capture(self.receipt_source(invoice));second=self.capture(self.receipt_source(invoice,4000))
        old=self.preview(second)
        self.assertFalse(old['can_apply']);self.assertIn('purchase_receipt_context_changed',{x['code'] for x in old['issues']})
        first_preview=self.preview(first);self.assertTrue(first_preview['can_apply'],first_preview)
        self.assertEqual(first_preview['stocks'][0]['validated_after_milli'],'144000')
        response,command=self.apply(first,first_preview);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(self.client.post(self.url(f'operations/{first.id}/integrate/'),command,format='json').data,response.data)
        self.assertEqual(self.apply(second,old)[0].status_code,409)
        response,_=self.apply(second);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(sum(StockMovement.objects.values_list('quantity_milli',flat=True)),240000)
        self.assertEqual((PurchaseReceipt.objects.count(),PurchaseReceiptLine.objects.count()),(2,2))
        self.assertEqual((SupplierDebt.objects.get().remaining_fcfa,SupplierLedgerEntry.objects.count(),CashMovement.objects.count()),(10000,1,0))
        known=self.preview(first);self.assertEqual(known['outcome'],'already_known');self.assertTrue(known['can_apply'],known)
        self.assertEqual(known['stocks'][0]['validated_after_milli'],'240000')

    def test_minimal_legacy_receipt_is_retained_but_cannot_invent_missing_cost_or_row_proof(self):
        _,invoice=self.native_purchase();source=self.receipt_source(invoice)
        for key in ('id','movement_id','product_id','unit_kind','cost_basis'):source.payload['receipt']['lines'][0].pop(key)
        capture=self.capture(source);preview=self.preview(capture)
        self.assertFalse(preview['can_apply']);self.assertIn('legacy_purchase_receipt_without_proof',{x['code'] for x in preview['issues']})
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        capture.refresh_from_db();self.assertEqual(capture.payload,source.payload)
        self.assertFalse(PurchaseReceipt.objects.exists());self.assertFalse(StockMovement.objects.exists())

    def test_changed_line_cost_factor_or_foreign_invoice_never_creates_a_receipt(self):
        _,invoice=self.native_purchase()
        for case in ('cost','base','product','document'):
            with self.subTest(case=case):
                source=self.receipt_source(invoice);row=source.payload['receipt']['lines'][0]
                if case=='cost':row['cost_basis']['invoice_total_fcfa']=9999;row['cost_basis']['total_cost_nanos_fcfa']='5999400000000'
                elif case=='base':row['base_quantity_milli']=72000;row['cost_basis']['invoice_base_quantity_milli']=120000
                elif case=='product':row['product_id']=str(uuid.uuid4())
                else:source.payload['receipt']['document_id']=source.payload['original_request']['document_id']=str(uuid.uuid4())
                preview=self.preview(self.capture(source));self.assertFalse(preview['can_apply'],preview)
        self.assertFalse(PurchaseReceipt.objects.exists());self.assertFalse(StockMovement.objects.exists())

    def test_missing_historical_charge_proof_blocks_and_existing_proof_keeps_exact_cump(self):
        _,invoice=self.native_purchase();source=self.receipt_source(invoice);basis=source.payload['receipt']['lines'][0]['cost_basis']
        basis['charge_allocations']=[{'allocation_id':str(uuid.uuid4()),'revision_id':str(uuid.uuid4()),'charge_id':str(uuid.uuid4()),'amount_fcfa':1001}]
        basis['total_cost_nanos_fcfa']='6600600000000'
        capture=self.capture(source);blocked=self.preview(capture)
        self.assertFalse(blocked['can_apply']);self.assertIn('missing_purchase_charge_allocation',{x['code'] for x in blocked['issues']})
        self.assertEqual(self.apply(capture,blocked)[0].status_code,409)
        self.assertFalse(PurchaseReceipt.objects.exists())
        charge={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':'2026-10-24T13:00:00Z',
            'reason':'Transport confirmé','label':'Transport','amount_fcfa':1001,
            'allocations':[{'line_id':invoice['lines'][0]['id'],'amount_fcfa':1001}]}
        created=self.client.post(self.purchase_url('invoice',invoice['id'],'charges/'),charge,format='json')
        self.assertEqual(created.status_code,201,created.content)
        allocation=PurchaseChargeAllocation.objects.get()
        source=self.receipt_source(invoice);basis=source.payload['receipt']['lines'][0]['cost_basis']
        basis.update(total_cost_nanos_fcfa='6600600000000',charge_allocations=[{'allocation_id':str(allocation.id),
            'revision_id':str(allocation.revision_id),'charge_id':str(allocation.revision.charge_id),'amount_fcfa':1001}])
        recovered=self.capture(source);response,_=self.apply(recovered);self.assertEqual(response.status_code,201,response.content)
        from stock.services.movement_costs import ordered_product_cost_events,value_ordered_movements
        state,_=value_ordered_movements(ordered_product_cost_events(self.product.id))
        self.assertEqual((state.quantity_milli,state.stock_value_nanos_fcfa,state.average_unit_cost_micros),(144000,6600600000000,45837500))
        self.assertEqual((SupplierDebt.objects.get().remaining_fcfa,CashMovement.objects.count()),(10000,0))
        PurchaseChargeAllocation.objects.filter(id=allocation.id).update(amount_fcfa=1000)
        inconsistent=self.preview(recovered);self.assertFalse(inconsistent['can_apply'],inconsistent)
        self.assertIn('operation_conflict',{x['code'] for x in inconsistent['issues']})

    def test_late_decision_failure_rolls_back_receipt_stock_origin_and_cost_proof(self):
        _,invoice=self.native_purchase();source=self.receipt_source(invoice);capture=self.capture(source)
        with connection.cursor() as cursor:cursor.execute("CREATE TEMP TRIGGER fail_purchase_receipt_review BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected arrival review failure'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(capture)
        finally:
            with connection.cursor() as cursor:cursor.execute('DROP TRIGGER fail_purchase_receipt_review')
        self.assertFalse(Activity.objects.filter(id=source.operation_id).exists())
        self.assertEqual((PurchaseReceipt.objects.count(),PurchaseReceiptLine.objects.count(),StockMovement.objects.count()),(0,0,0))
        self.assertEqual((SupplierDebt.objects.get().remaining_fcfa,SupplierLedgerEntry.objects.count(),CashMovement.objects.count()),(10000,1,0))

    def test_current_revoked_session_cannot_apply_even_a_previously_ready_receipt(self):
        _,invoice=self.native_purchase();source=self.receipt_source(invoice);capture=self.capture(source);preview=self.preview(capture)
        DeviceSession.objects.filter(installation__user=self.owner).update(revoked_at=datetime.now(timezone.utc))
        response,_=self.apply(capture,preview);self.assertEqual(response.status_code,401,response.content)
        self.assertEqual((PurchaseReceipt.objects.count(),StockMovement.objects.count()),(0,0))

    def test_a_late_charge_invalidates_preview_but_preserves_the_original_receipt_cost_proof(self):
        _,invoice=self.native_purchase();source=self.receipt_source(invoice);capture=self.capture(source);old=self.preview(capture)
        charge={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':'2026-10-24T13:00:00Z',
            'reason':'Transport arrivé après le journal','label':'Transport tardif','amount_fcfa':1001,
            'allocations':[{'line_id':invoice['lines'][0]['id'],'amount_fcfa':1001}]}
        created=self.client.post(self.purchase_url('invoice',invoice['id'],'charges/'),charge,format='json')
        self.assertEqual(created.status_code,201,created.content)
        response,_=self.apply(capture,old);self.assertEqual(response.status_code,409,response.content)
        current=self.preview(capture);self.assertTrue(current['can_apply'],current)
        self.assertIn('purchase_receipt_cost_context_revised',{x['code'] for x in current['notices']})
        response,_=self.apply(capture,current);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.data['business_receipt']['lines'][0]['cost_basis']['total_cost_nanos_fcfa'],'6000000000000')
        self.assertEqual(Activity.objects.get(id=source.operation_id).details['recovery']['source_snapshot'],current['snapshot'])
        self.assertEqual(self.preview(capture)['outcome'],'already_known')

    def test_foreign_charge_operation_is_refused_before_its_amount_is_exposed_or_stock_changes(self):
        _,invoice=self.native_purchase();source=self.receipt_source(invoice);capture=self.capture(source)
        charge={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':'2026-10-24T13:00:00Z',
            'reason':'Frais dont le lien sera altéré','label':'Frais confidentiels','amount_fcfa':987654,
            'allocations':[{'line_id':invoice['lines'][0]['id'],'amount_fcfa':987654}]}
        created=self.client.post(self.purchase_url('invoice',invoice['id'],'charges/'),charge,format='json')
        self.assertEqual(created.status_code,201,created.content)
        Activity.objects.filter(id=charge['operation_id']).update(shop=self.b)
        preview=self.preview(capture);self.assertFalse(preview['can_apply'])
        self.assertIn('purchase_receipt_cost_context_invalid',{row['code'] for row in preview['issues']})
        self.assertNotIn('987654',str(preview));self.assertNotIn('Frais confidentiels',str(preview))
        raw=self.native_body(source);raw.pop('document_id')
        raw.update(id=str(uuid.uuid4()),operation_id=str(uuid.uuid4()))
        response=self.client.post(self.purchase_url('invoice',invoice['id'],'receipts/'),raw,format='json')
        self.assertEqual((response.status_code,response.data.get('code')),(409,'origin_reference_conflict'),response.content)
        self.assertEqual((PurchaseReceipt.objects.count(),PurchaseReceiptLine.objects.count(),StockMovement.objects.count()),(0,0,0))

    def test_inconsistent_invoice_pack_blocks_recovery_before_stock_or_foreign_values_are_loaded(self):
        _,invoice=self.native_purchase();source=self.receipt_source(invoice);capture=self.capture(source);old=self.preview(capture)
        foreign=Product.objects.create(shop=self.b,name='Produit privé B',unit_kind='piece',unit_label='pièce',sale_price_fcfa=987654)
        pack=ProductPack.objects.create(product=foreign,name='Conditionnement privé B',base_quantity_milli=24000,sale_price_fcfa=987654)
        PurchaseLine.objects.filter(id=invoice['lines'][0]['id']).update(pack=pack)
        preview=self.preview(capture);self.assertFalse(preview['can_apply'],preview)
        self.assertIn('purchase_line_relation_conflict',{row['code'] for row in preview['issues']})
        self.assertEqual(preview['stocks'],[])
        for private in ('Produit privé B','Conditionnement privé B','987654'):self.assertNotIn(private,str(preview))
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        self.assertEqual((PurchaseReceipt.objects.count(),PurchaseReceiptLine.objects.count(),StockMovement.objects.count()),(0,0,0))
        capture.refresh_from_db();self.assertEqual(capture.payload,source.payload)
