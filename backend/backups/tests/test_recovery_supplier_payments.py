"""Recover actual supplier cash outflows and separate exact noncash allocations."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import uuid
from django.db import connection,IntegrityError
from django.test import TestCase,SimpleTestCase
from backups import models as backups
from backups.tests import test_recovery_supplier_dependencies as fixtures
from cash.models import CashMovement
from common.models import Activity
from identity.models import User,Membership,DeviceSession
from purchasing.models import Supplier,SupplierPayment,SupplierPaymentPart,SupplierPaymentAllocation,SupplierDebt,SupplierLedgerEntry,PurchaseDocument
from stock.models import StockMovement


class PaymentSources:
    def payment_source(self,supplier_id=None):
        identifier,operation,actor,device=[uuid.uuid4() for _ in range(4)]
        supplier_id=supplier_id or self.supplier_id;occurred=datetime(2026,10,24,12,tzinfo=timezone.utc)
        parts=[{'id':str(uuid.uuid4()),'method':method,'other_name':'','external_reference':'','amount_fcfa':amount}
            for method,amount in (('cash',4000),('orange',6000))]
        raw={'id':str(identifier),'operation_id':str(operation),'supplier_id':str(supplier_id),'installation_id':str(device),
            'occurred_at':occurred.isoformat(),'reason':'Versement réel fournisseur survivant','parts':deepcopy(parts)}
        snapshot={'id':str(identifier),'supplier_id':str(supplier_id),'operation_id':str(operation),'installation_id':str(device),
            'occurred_at':occurred.isoformat(),'created_at':(occurred+timedelta(seconds=3)).isoformat(),
            'amount_fcfa':10000,'unallocated_fcfa':10000,'parts':parts,'allocations':[]}
        return SimpleNamespace(operation_type='supplier_payment',operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=occurred,payload={'schema_version':1,'original_request':raw,'payment':snapshot})

    def allocation_source(self,payment_id,debt_ids=None,*,before=10000):
        operation,actor,device=[uuid.uuid4() for _ in range(3)];occurred=datetime(2026,10,25,11,tzinfo=timezone.utc)
        debt_ids=debt_ids or [debt['id'] for debt in self.debts]
        rows=[{'id':str(uuid.uuid4()),'debt_id':str(debt_id),'amount_fcfa':amount} for debt_id,amount in zip(debt_ids,(3000,4000),strict=True)]
        raw={'operation_id':str(operation),'payment_id':str(payment_id),'installation_id':str(device),
            'reason':'Affectation réellement confirmée','allocations':[{key:row[key] for key in ('debt_id','amount_fcfa')} for row in rows]}
        snapshot={'operation_id':str(operation),'supplier_id':str(self.supplier_id),'payment_id':str(payment_id),
            'allocated_fcfa':7000,'before_unallocated_fcfa':before,'after_unallocated_fcfa':before-7000,
            'recorded_at':occurred.isoformat(),'allocations':rows}
        return SimpleNamespace(operation_type='supplier_payment_allocation',operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=occurred,payload={'schema_version':1,'original_request':raw,'allocation':snapshot})


class RecoverySupplierPaymentSnapshotTests(SimpleTestCase,PaymentSources):
    def setUp(self):self.supplier_id=uuid.uuid4();self.debts=[{'id':str(uuid.uuid4())},{'id':str(uuid.uuid4())}]

    def test_initial_payment_preserves_each_part_and_only_a_separate_event_can_allocate_it(self):
        from backups.services.recovery_supplier_payment_snapshots import decode_supplier_payment,RecoverySupplierPaymentInvalid
        source=self.payment_source();self.assertEqual(decode_supplier_payment(source).snapshot['amount_fcfa'],10000)
        for case in ('total','available','part','bool','actor_device','time','created','already_allocated','extra'):
            with self.subTest(case=case):
                source=self.payment_source();snapshot=source.payload['payment']
                if case=='total':snapshot['amount_fcfa']=9999
                elif case=='available':snapshot['unallocated_fcfa']=9000
                elif case=='part':snapshot['parts'][0]['id']=str(uuid.uuid4())
                elif case=='bool':snapshot['parts'][0]['amount_fcfa']=True
                elif case=='actor_device':snapshot['installation_id']=str(uuid.uuid4())
                elif case=='time':snapshot['occurred_at']='2026-10-24T13:00:00Z'
                elif case=='created':snapshot['created_at']='2026-10-24T11:00:00Z'
                elif case=='already_allocated':snapshot['allocations']=[{'debt_id':str(uuid.uuid4()),'amount_fcfa':1}]
                else:snapshot['stock_milli']=1000
                with self.assertRaises(RecoverySupplierPaymentInvalid):decode_supplier_payment(source)

    def test_allocation_requires_exact_target_order_row_ids_and_original_before_after(self):
        from backups.services.recovery_supplier_payment_snapshots import decode_supplier_allocation,RecoverySupplierPaymentInvalid
        source=self.allocation_source(uuid.uuid4());self.assertEqual(decode_supplier_allocation(source).snapshot['allocated_fcfa'],7000)
        for case in ('total','after','before','negative','row_id','debt','order','device','time','extra'):
            with self.subTest(case=case):
                source=self.allocation_source(uuid.uuid4());snapshot=source.payload['allocation']
                if case=='total':snapshot['allocated_fcfa']=6999
                elif case=='after':snapshot['after_unallocated_fcfa']=2999
                elif case=='before':snapshot['before_unallocated_fcfa']=6999
                elif case=='negative':snapshot['allocations'][0]['amount_fcfa']=-1
                elif case=='row_id':snapshot['allocations'][1]['id']=snapshot['allocations'][0]['id']
                elif case=='debt':snapshot['allocations'][0]['debt_id']=str(uuid.uuid4())
                elif case=='order':snapshot['allocations'].reverse()
                elif case=='device':source.payload['original_request']['installation_id']=str(uuid.uuid4())
                elif case=='time':snapshot['recorded_at']='2026-10-25T12:00:00Z'
                else:snapshot['refund_fcfa']=1
                with self.assertRaises(RecoverySupplierPaymentInvalid):decode_supplier_allocation(source)


class RecoverySupplierPaymentTests(TestCase,PaymentSources):
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
    supplier_source=fixtures.SupplierSources.supplier_source
    opening_source=fixtures.SupplierSources.opening_source
    native_supplier=fixtures.RecoverySupplierDependencyTests.native_supplier
    native_opening=fixtures.RecoverySupplierDependencyTests.native_opening
    native_body=fixtures.RecoverySupplierDependencyTests.native_body
    native_url=fixtures.RecoverySupplierDependencyTests.native_url

    def setUp(self):
        fixtures.RecoverySupplierDependencyTests.setUp(self)
        self.supplier_id=self.native_supplier();self.debts=[self.native_opening(self.supplier_id)[1] for _ in range(2)]

    def pay_path(self,payment_id=None,suffix=''):
        return self.native_url(self.supplier_id,'payments/'+(str(payment_id)+'/' if payment_id else '')+suffix)

    def native_payment(self):
        source=self.payment_source();source.origin_user_id=self.owner.id
        source.origin_installation_id=DeviceSession.objects.get(installation__user=self.owner).installation_id
        source.payload['original_request']['installation_id']=str(source.origin_installation_id)
        source.payload['payment']['installation_id']=str(source.origin_installation_id)
        response=self.client.post(self.pay_path(),self.native_body(source),format='json');self.assertEqual(response.status_code,201,response.content)
        source.payload['payment']=deepcopy(response.data)
        return source,response.data

    def test_payment_then_allocation_recover_in_order_with_one_real_outflow_and_stable_rows(self):
        source=self.payment_source();payment_id=source.payload['payment']['id'];payment=self.capture(source)
        allocation_source=self.allocation_source(payment_id);allocation=self.capture(allocation_source,[source.operation_id])
        old=self.preview(allocation);self.assertFalse(old['can_apply'])
        preview=self.preview(payment);self.assertEqual(preview['cash_delta_by_method_fcfa'],{'cash':-4000,'orange':-6000})
        self.assertEqual(preview['new_supplier_advance_fcfa'],10000)
        response,command=self.apply(payment,preview);self.assertEqual(response.status_code,201,response.content)
        first=response.data;self.assertEqual(first['business_receipt']['installation_id'],str(source.origin_installation_id))
        self.assertEqual(self.client.post(self.url(f'operations/{payment.id}/integrate/'),command,format='json').data,first)
        self.assertEqual(sorted(CashMovement.objects.values_list('amount_fcfa',flat=True)),[-6000,-4000])
        for part in SupplierPaymentPart.objects.all():self.assertEqual(part.cash_movement.id,uuid.uuid5(part.id,'supplier-payment-cash'))
        self.assertEqual(self.apply(allocation,old)[0].status_code,409)
        response,command=self.apply(allocation);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.data['business_receipt']['after_unallocated_fcfa'],3000)
        self.assertEqual({row['id'] for row in response.data['business_receipt']['allocations']},{row['id'] for row in allocation_source.payload['allocation']['allocations']})
        self.assertTrue(all(row['actor_id']==str(allocation_source.origin_user_id) for row in response.data['business_receipt']['allocations']))
        self.assertEqual(self.client.post(self.url(f'operations/{allocation.id}/integrate/'),command,format='json').data,response.data)
        self.assertEqual((SupplierPayment.objects.get().unallocated_fcfa,SupplierPaymentAllocation.objects.count(),CashMovement.objects.count()),(3000,2,2))
        self.assertEqual(sorted(SupplierDebt.objects.values_list('remaining_fcfa',flat=True)),[4000,5000])
        self.assertEqual((SupplierLedgerEntry.objects.count(),backups.RecoveredOperationOrigin.objects.count()),(4,2))
        self.assertFalse(PurchaseDocument.objects.exists());self.assertFalse(StockMovement.objects.exists())
        detail=self.client.get(self.pay_path(payment_id));self.assertEqual(detail.status_code,200,detail.content)
        self.assertEqual(detail.data['installation_id'],str(source.origin_installation_id))
        self.assertTrue(all(row['actor_id']==str(allocation_source.origin_user_id) for row in detail.data['allocations']))

    def test_source_amount_is_not_capped_and_exact_allocation_with_different_available_balance_keeps_source(self):
        source=self.payment_source();response,_=self.apply(self.capture(source));self.assertEqual(response.status_code,201,response.content)
        payment=SupplierPayment.objects.get()
        earlier=self.client.post(self.pay_path(payment.id,'allocate/'),{'operation_id':str(uuid.uuid4()),
            'reason':'Autre affectation explicite déjà connue','allocations':[{'debt_id':self.debts[0]['id'],'amount_fcfa':1000}]},format='json')
        self.assertEqual(earlier.status_code,201,earlier.content)
        # Current quantity differs; exact original allocation remains fully possible.
        source=self.allocation_source(payment.id);capture=self.capture(source);response,_=self.apply(capture)
        self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.data['business_receipt']['after_unallocated_fcfa'],2000)
        operation=Activity.objects.get(id=source.operation_id)
        self.assertEqual(operation.details['recovery']['source_snapshot']['after_unallocated_fcfa'],3000)
        self.assertNotEqual(response.data['business_receipt']['recorded_at'],operation.details['recovery']['source_snapshot']['recorded_at'])
        self.assertEqual(SupplierLedgerEntry.objects.get(operation=operation).occurred_at,source.occurred_at)
        self.assertEqual(sum(CashMovement.objects.values_list('amount_fcfa',flat=True)),-10000)

    def test_changed_or_insufficient_current_debt_blocks_all_rows_without_losing_real_payment(self):
        source=self.payment_source();self.assertEqual(self.apply(self.capture(source))[0].status_code,201)
        allocation=self.allocation_source(source.payload['payment']['id']);capture=self.capture(allocation);old=self.preview(capture)
        SupplierDebt.objects.filter(id=self.debts[1]['id']).update(remaining_fcfa=3999)
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        current=self.preview(capture);self.assertFalse(current['can_apply']);self.assertEqual(self.apply(capture,current)[0].status_code,409)
        self.assertFalse(SupplierPaymentAllocation.objects.exists());self.assertEqual(SupplierPayment.objects.get().unallocated_fcfa,10000)
        self.assertEqual(SupplierDebt.objects.get(id=self.debts[0]['id']).remaining_fcfa,8000)
        self.assertEqual((CashMovement.objects.count(),sum(CashMovement.objects.values_list('amount_fcfa',flat=True))),(2,-10000))

    def test_late_review_failure_rolls_back_money_parts_advance_debts_rows_and_lineage(self):
        source=self.payment_source();capture=self.capture(source)
        with connection.cursor() as cursor:cursor.execute("CREATE TEMP TRIGGER fail_supplier_money BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected supplier money failure'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(capture)
        finally:
            with connection.cursor() as cursor:cursor.execute('DROP TRIGGER fail_supplier_money')
        self.assertFalse(SupplierPayment.objects.exists());self.assertFalse(SupplierPaymentPart.objects.exists());self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(backups.RecoveredOperationOrigin.objects.exists());self.assertEqual(SupplierLedgerEntry.objects.count(),2)
        self.assertEqual(self.apply(capture)[0].status_code,201)
        source=self.allocation_source(source.payload['payment']['id']);capture=self.capture(source)
        with connection.cursor() as cursor:cursor.execute("CREATE TEMP TRIGGER fail_supplier_allocation BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected supplier allocation failure'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(capture)
        finally:
            with connection.cursor() as cursor:cursor.execute('DROP TRIGGER fail_supplier_allocation')
        self.assertFalse(SupplierPaymentAllocation.objects.exists());self.assertEqual(SupplierPayment.objects.get().unallocated_fcfa,10000)
        self.assertTrue(all(value==8000 for value in SupplierDebt.objects.values_list('remaining_fcfa',flat=True)))
        self.assertEqual((SupplierLedgerEntry.objects.count(),CashMovement.objects.count(),backups.RecoveredOperationOrigin.objects.count()),(3,2,1))

    def test_native_payment_and_allocation_are_known_after_consumption_without_resetting_balances(self):
        source,first=self.native_payment();allocation=self.allocation_source(first['id'])
        allocation.origin_user_id=self.owner.id;allocation.origin_installation_id=DeviceSession.objects.get(installation__user=self.owner).installation_id
        allocation.payload['original_request']['installation_id']=str(allocation.origin_installation_id)
        raw={key:value for key,value in allocation.payload['original_request'].items() if key not in ('payment_id','installation_id')}
        response=self.client.post(self.pay_path(first['id'],'allocate/'),raw,format='json');self.assertEqual(response.status_code,201,response.content)
        second=response.data;snapshot=allocation.payload['allocation'];snapshot.update(recorded_at=second['recorded_at'],after_unallocated_fcfa=second['after_unallocated_fcfa'],
            allocations=[{key:row[key] for key in ('id','debt_id','amount_fcfa')} for row in second['allocations']])
        allocation.occurred_at=datetime.fromisoformat(second['recorded_at'].replace('Z','+00:00'))
        for item,expected in ((source,first),(allocation,second)):
            capture=self.capture(item);preview=self.preview(capture);self.assertEqual(preview['outcome'],'already_known',preview)
            self.assertTrue(preview['origin_installation_verified']);response,_=self.apply(capture,preview)
            self.assertEqual((response.status_code,response.data['business_receipt']),(201,expected))
        self.assertEqual((SupplierPayment.objects.get().unallocated_fcfa,CashMovement.objects.count(),backups.RecoveredOperationOrigin.objects.count()),(3000,2,0))

    def test_missing_and_foreign_references_permissions_and_captured_native_bypass_remain_blocked(self):
        source=self.payment_source();payment=self.capture(source)
        self.assertEqual(self.client.post(self.pay_path(),self.native_body(source),format='json').status_code,409)
        seller=User.objects.create_user(username='supplier-money-seller');member=Membership.objects.create(shop=self.a,user=seller)
        self.review(self.a,seller,'seller',membership=member,confirmed=True);api=self.authenticate(seller)
        self.assertEqual(api.generic('POST',self.url(f'operations/{payment.id}/integrate/'),b'invalid',content_type='application/json').status_code,403)
        self.assertEqual(self.client.get(self.url(f'operations/{payment.id}/integration-preview/',shop=self.b)).status_code,404)
        foreign=Supplier.objects.create(shop=self.b,name='Fournisseur privé B')
        source=self.payment_source(foreign.id);preview=self.preview(self.capture(source))
        self.assertFalse(preview['can_apply']);self.assertIsNone(preview['current_state']);self.assertNotIn(foreign.name,str(preview))
        allocation=self.allocation_source(uuid.uuid4());preview=self.preview(self.capture(allocation,[uuid.uuid4()]))
        self.assertFalse(preview['can_apply']);self.assertIsNone(preview['current_state'])
        self.assertTrue({'missing_payment','missing_dependencies'}<={row['code'] for row in preview['issues']})

    def test_native_derived_cash_uuid_collision_is_a_conflict_without_partial_payment(self):
        source=self.payment_source();part=source.payload['payment']['parts'][1]
        event=Activity.objects.create(shop=self.a,actor=self.owner,action='cash_opened',target_id=uuid.uuid4(),reason='Fonds réel déjà conservé')
        CashMovement.objects.create(id=uuid.uuid5(uuid.UUID(part['id']),'supplier-payment-cash'),shop=self.a,actor=self.owner,
            installation=DeviceSession.objects.get(installation__user=self.owner).installation,operation=event,manual_kind='opening',
            method='cash',amount_fcfa=1,occurred_at=source.occurred_at,reason='Fonds réel déjà conservé')
        response=self.client.post(self.pay_path(),self.native_body(source),format='json')
        self.assertEqual(response.status_code,409,response.content)
        self.assertFalse(SupplierPayment.objects.exists());self.assertFalse(SupplierPaymentPart.objects.exists())
        self.assertEqual((CashMovement.objects.count(),CashMovement.objects.get().amount_fcfa),(1,1))

    def test_recovered_receipts_survive_later_allocations_and_next_generation(self):
        source=self.payment_source();allocation=self.allocation_source(source.payload['payment']['id'])
        first=[]
        for item in (source,allocation):
            response,_=self.apply(self.capture(item));self.assertEqual(response.status_code,201,response.content);first.append(response.data['business_receipt'])
        response=self.client.post(self.pay_path(first[0]['id'],'allocate/'),{'operation_id':str(uuid.uuid4()),'reason':'Affectation postérieure explicite',
            'allocations':[{'debt_id':self.debts[0]['id'],'amount_fcfa':1000}]},format='json');self.assertEqual(response.status_code,201,response.content)
        self.current.current_slot=None;self.current.save();self.current=self.generation();self.registry=self.record(self.current)
        self.review(self.a,self.owner,'owner',confirmed=True)
        for item,expected in zip((source,allocation),first,strict=True):
            capture=self.capture(item);preview=self.preview(capture);self.assertEqual(preview['outcome'],'already_known',preview)
            response,_=self.apply(capture,preview);self.assertEqual(response.data['business_receipt'],expected)
        self.assertEqual((SupplierPayment.objects.count(),SupplierPayment.objects.get().unallocated_fcfa,SupplierPaymentAllocation.objects.count()),(1,2000,3))
        self.assertEqual((CashMovement.objects.count(),sum(CashMovement.objects.values_list('amount_fcfa',flat=True)),SupplierLedgerEntry.objects.count()),(2,-10000,5))

    def test_known_legacy_random_cash_uuid_is_retained_without_a_second_outflow(self):
        source,first=self.native_payment();cash=CashMovement.objects.first();legacy_id=uuid.uuid4()
        CashMovement.objects.filter(id=cash.id).update(id=legacy_id)
        capture=self.capture(source);preview=self.preview(capture);self.assertEqual(preview['outcome'],'already_known',preview)
        response,_=self.apply(capture,preview);self.assertEqual(response.data['business_receipt'],first)
        self.assertTrue(CashMovement.objects.filter(id=legacy_id).exists());self.assertEqual(CashMovement.objects.count(),2)

    def test_duplicate_reference_is_signalled_without_discarding_distinct_actual_payments(self):
        source=self.payment_source()
        for part in (source.payload['payment']['parts'][0],source.payload['original_request']['parts'][0]):
            part.update(method='other',other_name='Compte fournisseur',external_reference='REF-REUTILISEE')
        capture=self.capture(source);old=self.preview(capture);self.assertFalse(old['has_possible_duplicate'])
        other=self.payment_source()
        for part in (other.payload['payment']['parts'][0],other.payload['original_request']['parts'][0]):
            part.update(method='other',other_name='Compte fournisseur',external_reference='REF-REUTILISEE')
        response=self.client.post(self.pay_path(),self.native_body(other),format='json');self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        current=self.preview(capture);self.assertTrue(current['has_possible_duplicate']);response,_=self.apply(capture,current)
        self.assertEqual(response.status_code,201,response.content)
        self.assertEqual((SupplierPayment.objects.count(),CashMovement.objects.count(),sum(CashMovement.objects.values_list('amount_fcfa',flat=True))),(2,4,-20000))
        self.assertTrue(all(value==8000 for value in SupplierDebt.objects.values_list('remaining_fcfa',flat=True)))

    def test_known_payment_requires_immutable_parts_cash_and_ledger_not_only_a_matching_receipt(self):
        for case in ('part','cash','ledger','model'):
            with self.subTest(case=case):
                source=self.payment_source();capture=self.capture(source);response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
                payment=SupplierPayment.objects.get(id=source.payload['payment']['id'])
                if case=='part':payment.parts.filter(position=0).update(external_reference='Altérée')
                elif case=='cash':CashMovement.objects.filter(supplier_payment_part__payment=payment,method='cash').update(amount_fcfa=-3999)
                elif case=='ledger':SupplierLedgerEntry.objects.filter(operation=payment.operation).update(source_id=uuid.uuid4())
                else:SupplierPayment.objects.filter(id=payment.id).update(amount_fcfa=11000)
                preview=self.preview(capture);self.assertFalse(preview['can_apply'],preview)
                self.assertIn('operation_conflict',{row['code'] for row in preview['issues']});self.assertEqual(self.apply(capture,preview)[0].status_code,409)

    def test_insufficient_current_advance_and_foreign_second_debt_have_no_partial_effect(self):
        source=self.payment_source();self.assertEqual(self.apply(self.capture(source))[0].status_code,201)
        payment_id=source.payload['payment']['id'];allocation=self.allocation_source(payment_id);capture=self.capture(allocation)
        native=self.client.post(self.pay_path(payment_id,'allocate/'),{'operation_id':str(uuid.uuid4()),'reason':'Autre affectation de quatre mille',
            'allocations':[{'debt_id':self.debts[0]['id'],'amount_fcfa':4000}]},format='json');self.assertEqual(native.status_code,201,native.content)
        current=self.preview(capture);self.assertFalse(current['can_apply']);self.assertIn('supplier_advance_insufficient',{row['code'] for row in current['issues']})
        self.assertEqual(self.apply(capture,current)[0].status_code,409)
        foreign=Supplier.objects.create(shop=self.b,name='Identité fournisseur privée B')
        SupplierDebt.objects.filter(id=self.debts[1]['id']).update(supplier=foreign,shop=self.b)
        current=self.preview(capture);self.assertIn('missing_debt',{row['code'] for row in current['issues']})
        self.assertNotIn(foreign.name,str(current));self.assertEqual(self.apply(capture,current)[0].status_code,409)
        self.assertEqual((SupplierPaymentAllocation.objects.count(),SupplierPayment.objects.get().unallocated_fcfa),(1,6000))
        self.assertEqual((CashMovement.objects.count(),sum(CashMovement.objects.values_list('amount_fcfa',flat=True))),(2,-10000))

    def test_corrupt_payment_or_allocation_lineage_refuses_public_reads_before_exposing_source_identity(self):
        source=self.payment_source();pay_capture=self.capture(source);self.assertEqual(self.apply(pay_capture)[0].status_code,201)
        allocated=self.allocation_source(source.payload['payment']['id']);alloc_capture=self.capture(allocated)
        self.assertEqual(self.apply(alloc_capture)[0].status_code,201)
        paths=(self.pay_path(),self.pay_path(source.payload['payment']['id']),self.native_url(self.supplier_id,'statement/'))
        for capture in (pay_capture,alloc_capture):
            backups.RecoveryJournalOperation.objects.filter(id=capture.id).update(shop=self.b)
            for language in ('fr','en'):
                for path in paths:
                    response=self.client.get(path,HTTP_ACCEPT_LANGUAGE=language)
                    self.assertEqual((response.status_code,response.data['code']),(409,'origin_reference_conflict'),response.content)
            backups.RecoveryJournalOperation.objects.filter(id=capture.id).update(shop=self.a)

    def test_allocation_capture_cannot_bypass_review_and_refused_current_owner_cannot_integrate(self):
        source=self.payment_source();self.assertEqual(self.apply(self.capture(source))[0].status_code,201)
        allocation=self.allocation_source(source.payload['payment']['id']);capture=self.capture(allocation)
        raw={key:value for key,value in allocation.payload['original_request'].items() if key not in ('payment_id','installation_id')}
        self.assertEqual(self.client.post(self.pay_path(source.payload['payment']['id'],'allocate/'),raw,format='json').status_code,409)
        preview=self.preview(capture);review=backups.RestoreAccessReview.objects.get(generation=self.current,shop=self.a,user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=1,version=2,state='refused',reason='Qualité actuelle retirée')
        self.assertEqual(self.apply(capture,preview)[0].status_code,404);self.assertFalse(SupplierPaymentAllocation.objects.exists())

    def test_live_preview_receipts_history_and_blocked_context_match_openapi_with_bilingual_errors(self):
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
        source=self.payment_source();allocated=self.allocation_source(source.payload['payment']['id'])
        for item in (source,allocated):
            capture=self.capture(item);preview=self.client.get(self.url(f'operations/{capture.id}/integration-preview/'))
            self.assertEqual(preview.status_code,200,preview.content);check(preview,'/integration-preview/')
            response,_=self.apply(capture,preview.data);self.assertEqual(response.status_code,201,response.content);check(response,'/integrate/','post')
        for path,ending in ((self.pay_path(),'/suppliers/{supplier_id}/payments/'),(self.pay_path(source.payload['payment']['id']),'/suppliers/{supplier_id}/payments/{id}/')):
            response=self.client.get(path);self.assertEqual(response.status_code,200,response.content);check(response,ending)
        impossible=self.allocation_source(source.payload['payment']['id']);capture=self.capture(impossible)
        blocked=self.client.get(self.url(f'operations/{capture.id}/integration-preview/'));self.assertFalse(blocked.data['can_apply']);check(blocked,'/integration-preview/')
        invalid=self.payment_source();invalid.payload['payment']['amount_fcfa']=1;capture=self.capture(invalid)
        for language,detail in (('en','Recovery supplier payment or allocation snapshot is inconsistent.'),('fr','Les données figées du paiement fournisseur ou de son affectation sont incohérentes.')):
            response=self.client.get(self.url(f'operations/{capture.id}/integration-preview/'),HTTP_ACCEPT_LANGUAGE=language)
            self.assertEqual((response.status_code,str(response.data['detail'])),(400,detail))
