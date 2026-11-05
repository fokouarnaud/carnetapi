"""Lose two actual partial arrivals and recover their physical facts after replacement."""
from datetime import datetime,timezone
import json
import uuid

from verification.supplier_payment_recovery_flow import record_supplier_money_flow,recover_supplier_money_flow,audit_supplier_money_t0


def record_purchase_receipt_flow(request,*,shop_id,actor_id,installation_id,baseline=None,documents=None):
    survivor=record_supplier_money_flow(request,shop_id=shop_id,actor_id=actor_id,installation_id=installation_id,baseline=baseline,documents=documents)
    invoice=next(row for row in survivor['ordered_operations'] if row['operation_type']=='purchase_invoice')
    frozen=invoice['payload']['purchase'];line=frozen['lines'][0]
    endpoint=f"/api/v1/shops/{shop_id}/suppliers/{survivor['supplier_id']}/invoices/{frozen['id']}/receipts/"
    arrivals=[];receipts=[]
    for quantity,base,before,total in ((6000,144000,0,'60000000000000'),(4000,96000,144000,'40000000000000')):
        raw={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':datetime.now(timezone.utc).isoformat(),
            'reason':'Marchandises réellement arrivées T1','lines':[{'line_id':line['id'],'quantity_milli':quantity}]}
        status,receipt,_=request(endpoint,data=raw);assert status==201,(status,receipt)
        row=receipt['lines'][0]
        assert row['base_quantity_milli']==base and row['product_id']==line['product_id']
        assert row['cost_basis']=={'invoice_total_fcfa':100000,'invoice_base_quantity_milli':240000,
            'received_before_base_milli':before,'total_cost_nanos_fcfa':total,'charge_allocations':[]},row
        assert row['id']==str(uuid.uuid5(uuid.UUID(raw['id']),'purchase-receipt-line:'+line['id']))
        assert row['movement_id']==str(uuid.uuid5(uuid.UUID(raw['id']),'purchase-stock:'+line['id']))
        arrivals.append({'operation_id':raw['operation_id'],'operation_type':'purchase_receipt','origin_user_id':str(actor_id),
            'origin_installation_id':str(installation_id),'occurred_at':receipt['occurred_at'],'origin_recorded_at':receipt['created_at'],
            'dependencies':[invoice['operation_id']],'payload':{'schema_version':1,
                'original_request':{**raw,'document_id':frozen['id'],'installation_id':str(installation_id)},'receipt':receipt}})
        receipts.append(receipt)
        status,detail,_=request(endpoint.removesuffix('receipts/'))
        assert status==200 and detail['lines'][0]['received_quantity_milli']==quantity+(6000 if quantity==4000 else 0),(status,detail)
    for entry,receipt in zip(arrivals,receipts,strict=True):
        raw={key:value for key,value in entry['payload']['original_request'].items() if key not in ('document_id','installation_id')}
        assert request(endpoint,data=raw)[:2]==(200,receipt)
    survivor['purchase_receipt_entries']=arrivals
    return survivor


def recover_purchase_receipt_flow(request,*,shop_id,foreign_shop_id,session_id,credential,survivor):
    summary=recover_supplier_money_flow(request,shop_id=shop_id,foreign_shop_id=foreign_shop_id,session_id=session_id,credential=credential,survivor=survivor)
    prefix=f'/api/v1/shops/{shop_id}/';arrivals=survivor['purchase_receipt_entries']
    journal={'operation_id':str(uuid.uuid4()),'format_version':1,'shop_id':str(shop_id),'session_id':session_id,
        'reason':'Réceptions physiques survivantes vérifiées','operations':list(reversed(arrivals))}
    status,captured,_=request(prefix+'recovery/journals/',credential,journal)
    assert status==201 and captured['operation_count']==2 and captured['business_effects_created']==0,(status,captured)
    assert request(prefix+'recovery/journals/',credential,journal)[:2]==(200,captured)
    captures={row['operation_id']:row['capture_id'] for row in captured['operations']}
    def path(entry):return prefix+f"recovery/operations/{captures[entry['operation_id']]}/"
    def preview(entry):
        status,result,_=request(path(entry)+'integration-preview/',credential);assert status==200,(status,result)
        return result
    def apply(entry,body):
        command={'operation_id':str(uuid.uuid4()),'previous_version':body['version'],'preview_token':body['preview_token'],
            'reason':'Quantités, identité et preuve du coût historique vérifiées'}
        status,result,_=request(path(entry)+'integrate/',credential,command)
        return status,result,command
    old=preview(arrivals[1]);assert not old['can_apply'] and 'purchase_receipt_context_changed' in {row['code'] for row in old['issues']}
    assert request(f"/api/v1/shops/{foreign_shop_id}/recovery/operations/{captures[arrivals[0]['operation_id']]}/integration-preview/",credential)[0]==404
    for index,entry in enumerate(arrivals):
        if index:assert apply(entry,old)[0]==409
        current=preview(entry);assert current['can_apply'] and current['cash_delta_by_method_fcfa']=={},current
        assert current['new_supplier_debt_fcfa']==0 and current['stocks'][0]['validated_after_milli']==str(144000 if index==0 else 240000)
        status,result,command=apply(entry,current);assert status==201,(status,result)
        expected=entry['payload']['receipt'];actual=result['business_receipt']
        assert {key:value for key,value in actual.items() if key!='created_at'}=={key:value for key,value in expected.items() if key!='created_at'}
        assert request(path(entry)+'integrate/',credential,command)[:2]==(200,result)
        known=preview(entry);assert known['can_apply'] and known['outcome']=='already_known'
        assert apply(entry,known)[1]['business_receipt']==actual
        endpoint=prefix+f"suppliers/{survivor['supplier_id']}/invoices/{expected['document_id']}/receipts/"
        raw={key:value for key,value in entry['payload']['original_request'].items() if key not in ('document_id','installation_id')}
        assert request(endpoint,credential,raw)[0]==409
    status,listing,_=request(endpoint,credential)
    assert status==200 and listing['count']==2 and {row['id'] for row in listing['results']}=={row['payload']['receipt']['id'] for row in arrivals},(status,listing)
    status,invoice,_=request(endpoint.removesuffix('receipts/'),credential)
    assert status==200 and invoice['lines'][0]['received_quantity_milli']==10000 and invoice['lines'][0]['awaiting_quantity_milli']==0
    assert invoice['debt']['remaining_fcfa']==70000
    status,cash,_=request(prefix+'cash/',credential)
    assert status==200 and cash['count']==2 and cash['totals_by_method_fcfa']=={'cash':-10000,'orange':-20000}
    return {**summary,'lost_t1_purchase_receipts_recovered':2,'purchase_physical_stock_milli':240000,
        'purchase_receipt_rows_and_movements_preserved':True,'purchase_receipt_exact_cost_proof_preserved':True,
        'purchase_receipt_second_partial_waits_for_first':True,'purchase_receipts_create_no_debt_or_cash':True}


def audit_purchase_receipt_t0(db,survivor):
    audit_supplier_money_t0(db,survivor)
    for table in ('core_purchasereceipt','core_purchasereceiptline'):assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==0


def audit_purchase_receipt_flow(db,survivor):
    for table,count in (('core_supplier',1),('core_purchasedocument',2),('core_purchaseline',2),('core_supplierdebt',1),
            ('core_supplierpayment',1),('core_supplierpaymentpart',2),('core_supplierpaymentallocation',1),('core_supplierledgerentry',3),
            ('core_purchasereceipt',2),('core_purchasereceiptline',2),('core_stockmovement',2),
            ('backups_recoveredoperationorigin',7),('backups_recoveryintegrationdecision',14),('core_suppliercredit',0),('core_supplierrefund',0)):
        assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==count,table
    assert db.execute('SELECT SUM(quantity_milli) FROM core_stockmovement').fetchone()[0]==240000
    assert db.execute('SELECT COUNT(*),SUM(amount_fcfa) FROM core_cashmovement').fetchone()==(2,-30000)
    assert db.execute('SELECT amount_fcfa,remaining_fcfa FROM core_supplierdebt').fetchone()==(100000,70000)
    assert db.execute('SELECT amount_fcfa,unallocated_fcfa FROM core_supplierpayment').fetchone()==(30000,0)
    for entry in survivor['purchase_receipt_entries']:
        source=entry['payload']['receipt'];row=source['lines'][0]
        assert db.execute('SELECT quantity_milli,base_quantity_milli,movement_id FROM core_purchasereceiptline WHERE id=?',
            [uuid.UUID(row['id']).hex]).fetchone()==(row['quantity_milli'],row['base_quantity_milli'],uuid.UUID(row['movement_id']).hex)
        details=json.loads(db.execute('SELECT details FROM core_activity WHERE id=?',[uuid.UUID(entry['operation_id']).hex]).fetchone()[0])
        assert details['recovery']['source_snapshot']==source
        assert details['response']['lines']==source['lines']
    from stock.services.movement_costs import value_ordered_movements,ordered_product_cost_events
    from django.db import connections
    try:
        product_id=survivor['purchase_receipt_entries'][0]['payload']['receipt']['lines'][0]['product_id']
        state,_=value_ordered_movements(ordered_product_cost_events(product_id))
        assert state.quantity_milli==240000 and state.stock_value_nanos_fcfa==100000000000000
    finally:connections.close_all()
