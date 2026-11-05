"""Four real supplier T1 facts survive T0; later settlement is an explicit new action."""
from datetime import datetime,timezone
import json
import uuid


def record_supplier_flow(request,*,shop_id,actor_id,installation_id):
    prefix=f'/api/v1/shops/{shop_id}/suppliers/';operations=[];receipts=[]
    occurred=datetime.now(timezone.utc).isoformat()
    raw={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'name':'Fournisseur réel T1','phone':'+237600000034'}
    status,supplier,_headers=request(prefix,data=raw);assert status==201,(status,supplier)
    operations.append({'operation_id':raw['operation_id'],'operation_type':'supplier_creation','origin_user_id':str(actor_id),
        'origin_installation_id':str(installation_id),'occurred_at':occurred,'origin_recorded_at':datetime.now(timezone.utc).isoformat(),
        'payload':{'schema_version':1,'original_request':{**raw,'installation_id':str(installation_id)},'supplier':supplier},'dependencies':[]})
    receipts.append(supplier)
    for kind,amount in (('debt',8000),('advance',3000),('credit',2000)):
        raw={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':datetime.now(timezone.utc).isoformat(),
            'amount_fcfa':amount,'reason':'Solde antérieur T1 confirmé distinctement'}
        if kind!='debt':raw['balance_kind']=kind
        status,receipt,_headers=request(prefix+supplier['id']+'/'+('debts/' if kind=='debt' else 'opening-balances/'),data=raw)
        assert status==201,(status,receipt)
        snapshot={key:receipt[key] for key in (('id','document_id','amount_fcfa','remaining_fcfa','occurred_at','created_at')
            if kind=='debt' else ('id','kind','return_id','reference_fcfa','amount_fcfa','applied_to_origin_fcfa','remaining_fcfa','occurred_at','created_at'))}
        snapshot['supplier_id']=supplier['id']
        operations.append({'operation_id':raw['operation_id'],'operation_type':'supplier_opening_debt' if kind=='debt' else 'supplier_opening_balance',
            'origin_user_id':str(actor_id),'origin_installation_id':str(installation_id),'occurred_at':receipt['occurred_at'],
            'origin_recorded_at':receipt['created_at'],'dependencies':[operations[0]['operation_id']],
            'payload':{'schema_version':1,'original_request':{**raw,'supplier_id':supplier['id'],'installation_id':str(installation_id)},
                'opening_debt' if kind=='debt' else 'opening_balance':snapshot}})
        receipts.append(receipt)
    return {'operations':[operations[2],operations[1],operations[3],operations[0]],'ordered_operations':operations,
        'receipts':receipts,'supplier_id':supplier['id']}


def recover_supplier_flow(request,*,shop_id,foreign_shop_id,session_id,credential,survivor):
    prefix=f'/api/v1/shops/{shop_id}/';supplier_path=prefix+'suppliers/'+survivor['supplier_id']+'/'
    assert request(supplier_path,credential)[0]==404
    journal={'operation_id':str(uuid.uuid4()),'format_version':1,'shop_id':str(shop_id),'session_id':session_id,
        'reason':'Fournisseur et ouvertures survivants contrôlés','operations':survivor['operations']}
    status,captured,_headers=request(prefix+'recovery/journals/',credential,journal)
    assert status==201 and captured['operation_count']==4 and captured['business_effects_created']==0,(status,captured)
    assert request(prefix+'recovery/journals/',credential,journal)[:2]==(200,captured)
    captures={row['operation_id']:row['capture_id'] for row in captured['operations']}
    def path(operation):return prefix+f'recovery/operations/{captures[operation]}/'
    def preview(operation):
        status,body,_headers=request(path(operation)+'integration-preview/',credential);assert status==200,(status,body)
        return body
    def apply(operation,body):
        command={'operation_id':str(uuid.uuid4()),'previous_version':body['version'],'preview_token':body['preview_token'],
            'reason':'Identité et faits initiaux fournisseur contrôlés'}
        status,receipt,_headers=request(path(operation)+'integrate/',credential,command)
        return status,receipt,command
    debt_operation=survivor['ordered_operations'][1]['operation_id'];old=preview(debt_operation)
    assert not old['can_apply'] and old['current_state'] is None
    assert {row['code'] for row in old['issues']}=={'missing_supplier','missing_dependencies'}
    assert request(f"/api/v1/shops/{foreign_shop_id}/recovery/operations/{captures[debt_operation]}/integration-preview/",credential)[0]==404
    acknowledgements=[]
    for index,(entry,source) in enumerate(zip(survivor['ordered_operations'],survivor['receipts'],strict=True)):
        operation=entry['operation_id']
        if index==1:assert apply(operation,old)[0]==409
        current=preview(operation);assert current['cash_delta_by_method_fcfa']=={} and current['stocks']==[]
        assert current['new_debt_fcfa']==0 and current['new_customer_credit_fcfa']==0
        if index==0:assert 'declared_supplier_creation_time' in {row['code'] for row in current['notices']}
        else:
            expected=[0,0,0];expected[index-1]=source['amount_fcfa']
            assert [current[key] for key in ('new_supplier_debt_fcfa','new_supplier_advance_fcfa','new_supplier_credit_fcfa')]==expected
        status,receipt,command=apply(operation,current);assert status==201,(status,receipt)
        actual=receipt['business_receipt'];acknowledgements.append(actual)
        assert {key:value for key,value in actual.items() if key!='created_at'}=={key:value for key,value in source.items() if key!='created_at'}
        if index:assert actual['created_at']!=source['created_at']
        assert request(path(operation)+'integrate/',credential,command)[:2]==(200,receipt)
    status,statement,_headers=request(supplier_path+'statement/',credential)
    assert status==200 and statement['count']==3,(status,statement)
    assert [statement[key] for key in ('closing_debt_fcfa','closing_advance_fcfa','closing_credit_fcfa','closing_balance_fcfa')]==[8000,3000,2000,3000]
    origin={entry['operation_id']:entry['origin_user_id'] for entry in survivor['ordered_operations'][1:]}
    assert {row['operation_id']:row['actor_id'] for row in statement['results']}==origin
    # These are new explicit actions after recovery, not claimed recovery of lost settlements.
    debt,advance,credit=acknowledgements[1:]
    allocation={'operation_id':str(uuid.uuid4()),'reason':'Affecter explicitement après reprise',
        'allocations':[{'debt_id':debt['id'],'amount_fcfa':1000}]}
    status,allocated,_headers=request(supplier_path+f"credits/{advance['id']}/allocate/",credential,allocation)
    assert status==201,(status,allocated)
    assert request(supplier_path+f"credits/{advance['id']}/allocate/",credential,allocation)[:2]==(200,allocated)
    refund={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':datetime.now(timezone.utc).isoformat(),
        'reason':'Remboursement effectivement reçu après reprise','method':'orange','amount_fcfa':500}
    status,refunded,_headers=request(supplier_path+f"credits/{credit['id']}/refund/",credential,refund)
    assert status==201,(status,refunded)
    assert request(supplier_path+f"credits/{credit['id']}/refund/",credential,refund)[:2]==(200,refunded)
    for entry,first in zip(survivor['ordered_operations'],acknowledgements,strict=True):
        operation=entry['operation_id'];current=preview(operation);assert current['outcome']=='already_known',current
        assert all(current[key]==0 for key in ('new_supplier_debt_fcfa','new_supplier_advance_fcfa','new_supplier_credit_fcfa'))
        status,receipt,_command=apply(operation,current);assert status==201 and receipt['business_receipt']==first,(status,receipt)
    status,statement,_headers=request(supplier_path+'statement/',credential)
    assert status==200 and statement['count']==5
    assert [statement[key] for key in ('closing_debt_fcfa','closing_advance_fcfa','closing_credit_fcfa','closing_balance_fcfa')]==[7000,2000,1500,3500]
    status,cash,_headers=request(prefix+'cash/',credential)
    assert status==200 and cash['count']==1 and cash['results'][0]['amount_fcfa']==500
    return {'native_t1_supplier_dependencies_recovered':4,'supplier_opening_positions_distinct':True,
        'supplier_creation_time_declared_not_fabricated':True,'post_recovery_explicit_allocation_fcfa':1000,
        'post_recovery_explicit_refund_fcfa':500,'remaining_supplier_debt_fcfa':7000,'remaining_supplier_advance_fcfa':2000,
        'remaining_supplier_credit_fcfa':1500,'supplier_flow_net_cash_fcfa':500,'no_purchase_stock_or_payment_created_by_openings':True}


def audit_supplier_t0(db):
    for table in ('core_supplier','core_supplierdebt','core_suppliercredit','core_supplierledgerentry','core_cashmovement','core_purchasedocument','core_stockmovement'):
        assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==0,table


def audit_supplier_flow(db,survivor):
    for table,count in (('core_supplier',1),('core_supplierdebt',1),('core_suppliercredit',2),('core_supplierledgerentry',5),
            ('core_suppliercreditallocation',1),('core_supplierrefund',1),('backups_recoveredoperationorigin',4),
            ('backups_recoveryintegrationdecision',8),('core_purchasedocument',0),('core_supplierpayment',0),('core_supplierreturn',0),
            ('core_stockmovement',0),('core_customer',0)):
        assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==count,table
    assert db.execute('SELECT COUNT(*),SUM(amount_fcfa) FROM core_cashmovement').fetchone()==(1,500)
    assert db.execute('SELECT remaining_fcfa FROM core_supplierdebt').fetchone()[0]==7000
    assert db.execute('SELECT kind,remaining_fcfa FROM core_suppliercredit ORDER BY kind').fetchall()==[('opening_advance',2000),('opening_credit',1500)]
    profile=survivor['ordered_operations'][0]
    details=json.loads(db.execute('SELECT details FROM core_activity WHERE id=?',[uuid.UUID(profile['operation_id']).hex]).fetchone()[0])
    assert datetime.fromisoformat(details['recovery']['source_occurred_at'].replace('Z','+00:00'))==datetime.fromisoformat(profile['occurred_at'])
