"""A native receipt's SQL row order cannot change its line identities or atomicity."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import uuid
from unittest.mock import patch

from django.db import connection,IntegrityError,transaction
from django.test import TestCase
from django.utils import timezone

from backups import models as backups
from backups.tests import test_recovery_purchase_receipts as fixtures
from catalogue.models import Product
from cash.models import CashMovement
from common.models import Activity
from identity.models import DeviceSession
from purchasing.models import PurchaseReceipt,PurchaseReceiptLine,SupplierDebt
from stock.models import StockMovement


class RecoveryPurchaseReceiptMultilineTests(TestCase):
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
    apply=fixtures.RecoveryPurchaseReceiptTests.apply
    native_url=fixtures.RecoveryPurchaseReceiptTests.native_url
    native_body=fixtures.RecoveryPurchaseReceiptTests.native_body
    native_supplier=fixtures.RecoveryPurchaseReceiptTests.native_supplier
    supplier_source=fixtures.RecoveryPurchaseReceiptTests.supplier_source
    purchase_url=fixtures.RecoveryPurchaseReceiptTests.purchase_url

    def native_lost_receipt(self):
        second=Product.objects.create(shop=self.a,name='Deuxième produit reçu',unit_kind='piece',unit_label='pièce',sale_price_fcfa=100)
        receipt_id=uuid.uuid4()
        line_ids=sorted([uuid.uuid4(),uuid.uuid4()],key=lambda value:uuid.uuid5(receipt_id,'purchase-receipt-line:'+str(value)),reverse=True)
        invoice={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':'2026-10-24T12:00:00Z',
            'reason':'Facture multiligne réelle','lines':[{'id':str(identifier),'product_id':str(product.id),
                'quantity_milli':10000,'unit_price_fcfa':100} for identifier,product in zip(line_ids,(self.product,second),strict=True)]}
        response=self.client.post(self.purchase_url(),invoice,format='json');self.assertEqual(response.status_code,201,response.content)
        raw={'id':str(receipt_id),'operation_id':str(uuid.uuid4()),'occurred_at':'2026-10-25T12:00:00Z',
            'reason':'Deux lignes physiquement reçues','lines':[{'line_id':str(line_ids[0]),'quantity_milli':6000},
                {'line_id':str(line_ids[1]),'quantity_milli':4000}]}
        endpoint=self.purchase_url('invoice',invoice['id'],'receipts/')
        # This is an actual HTTP receipt. Only its clock is controlled. Rolling
        # back its savepoint models a surviving phone copy absent from T0.
        with transaction.atomic():
            with patch('django.utils.timezone.now',return_value=timezone.now()):
                response=self.client.post(endpoint,raw,format='json')
            self.assertEqual(response.status_code,201,response.content)
            source_receipt=deepcopy(response.data)
            self.assertEqual(PurchaseReceiptLine.objects.values('created_at').distinct().count(),1)
            transaction.set_rollback(True)
        requested=[row['line_id'] for row in raw['lines']];returned=[row['line_id'] for row in source_receipt['lines']]
        self.assertEqual(returned,list(reversed(requested)))
        self.assertEqual((PurchaseReceipt.objects.count(),StockMovement.objects.count()),(0,0))
        live=DeviceSession.objects.get(installation__user=self.owner)
        return SimpleNamespace(operation_type='purchase_receipt',operation_id=uuid.UUID(raw['operation_id']),
            origin_user_id=self.owner.id,origin_installation_id=live.installation_id,
            occurred_at=datetime.fromisoformat(raw['occurred_at'].replace('Z','+00:00')),
            payload={'schema_version':1,'original_request':{**raw,'document_id':invoice['id'],'installation_id':str(live.installation_id)},
                'receipt':source_receipt})

    def test_native_multiline_copy_with_tied_timestamps_recovers_by_line_identity(self):
        source=self.native_lost_receipt();capture=self.capture(source);preview=self.preview(capture)
        self.assertTrue(preview['can_apply'],preview)
        response,command=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)
        receipt=response.data['business_receipt']
        self.assertEqual(receipt['lines'],source.payload['receipt']['lines'])
        self.assertEqual(Activity.objects.get(id=source.operation_id).details['request']['lines'],source.payload['original_request']['lines'])
        self.assertEqual((PurchaseReceipt.objects.count(),PurchaseReceiptLine.objects.count(),StockMovement.objects.count()),(1,2,2))
        self.assertEqual(sorted(StockMovement.objects.values_list('quantity_milli',flat=True)),[4000,6000])
        self.assertEqual((SupplierDebt.objects.get().remaining_fcfa,CashMovement.objects.count()),(2000,0))
        self.assertEqual(self.client.post(self.url(f'operations/{capture.id}/integrate/'),command,format='json').data,response.data)
        self.assertEqual(self.preview(capture)['outcome'],'already_known')

    def test_invalid_second_movement_rolls_back_both_receipt_lines_and_all_effects(self):
        source=self.native_lost_receipt();capture=self.capture(source);preview=self.preview(capture)
        self.assertTrue(preview['can_apply'],preview)
        second_product=uuid.UUID(source.payload['receipt']['lines'][-1]['product_id']).hex
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER reject_second_receipt_row BEFORE INSERT ON core_stockmovement WHEN NEW.product_id='"+second_product+"' BEGIN SELECT RAISE(ABORT,'invalid second arrival line'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(capture,preview)
        finally:
            with connection.cursor() as cursor:cursor.execute('DROP TRIGGER reject_second_receipt_row')
        self.assertEqual((PurchaseReceipt.objects.count(),PurchaseReceiptLine.objects.count(),StockMovement.objects.count()),(0,0,0))
        self.assertFalse(Activity.objects.filter(id=source.operation_id).exists())
        self.assertEqual((backups.RecoveredOperationOrigin.objects.count(),backups.RecoveryIntegrationDecision.objects.count()),(0,0))
        self.assertEqual((SupplierDebt.objects.get().remaining_fcfa,CashMovement.objects.count()),(2000,0))

    def test_line_identity_matching_keeps_duplicate_uuid_and_amount_checks_strict(self):
        from backups.services.recovery_purchase_receipts import decode_purchase_receipt,RecoveryPurchaseReceiptInvalid
        original=self.native_lost_receipt()
        for case in ('duplicate-line','foreign-line','quantity','row-id','movement-id','cost'):
            with self.subTest(case=case):
                source=deepcopy(original);first,second=source.payload['receipt']['lines']
                if case=='duplicate-line':second['line_id']=first['line_id']
                elif case=='foreign-line':second['line_id']=str(uuid.uuid4())
                elif case=='quantity':second['quantity_milli']+=1
                elif case=='row-id':second['id']=first['id']
                elif case=='movement-id':second['movement_id']=first['movement_id']
                else:second['cost_basis']['total_cost_nanos_fcfa']='0'
                with self.assertRaises(RecoveryPurchaseReceiptInvalid):decode_purchase_receipt(source)
