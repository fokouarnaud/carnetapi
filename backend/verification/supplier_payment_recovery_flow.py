"""Real T1 supplier, debt, payment and allocation survive a real T0 replacement."""
from datetime import datetime,timezone
import json
import uuid


def prepare_supplier_invoice_baseline(*,shop,owner,session,product):
    from rest_framework.test import APIClient
    from rest_framework_simplejwt.tokens import AccessToken
    token=AccessToken.for_user(owner);token['session_id']=str(session.id)
    api=APIClient();api.credentials(HTTP_AUTHORIZATION='Bearer '+str(token))
    prefix=f'/api/v1/shops/{shop.id}/suppliers/'
    response=api.post(prefix,{'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'name':'Facture fournisseur T0'},format='json')
    assert response.status_code==201,response.content
    supplier=response.data
    invoice_operation_id=str(uuid.uuid4())
    response=api.post(prefix+supplier['id']+'/invoices/',{'id':str(uuid.uuid4()),'operation_id':invoice_operation_id,
        'occurred_at':datetime.now(timezone.utc).isoformat(),'reason':'Facture validée conservée dans T0','external_reference':'FACTURE-T0',
        'lines':[{'id':str(uuid.uuid4()),'product_id':str(product.id),'quantity_milli':100000,'unit_price_fcfa':1000}]},format='json')
    assert response.status_code==201 and response.data['total_fcfa']==100000,response.content
    invoice=response.data
    return {'supplier_id':supplier['id'],'invoice_id':invoice['id'],'debt_id':invoice['debt']['id'],
        'invoice_operation_id':invoice_operation_id,'actor_id':str(owner.id)}


def record_supplier_money_flow(request,*,shop_id,actor_id,installation_id,baseline=None,documents=None):
    prefix=f'/api/v1/shops/{shop_id}/suppliers/';operations=[];receipts=[]
    def entry(raw,kind,key,snapshot,occurred,recorded,dependencies):
        return {'operation_id':raw['operation_id'],'operation_type':kind,'origin_user_id':str(actor_id),
            'origin_installation_id':str(installation_id),'occurred_at':occurred,'origin_recorded_at':recorded,
            'dependencies':dependencies,'payload':{'schema_version':1,'original_request':raw,key:snapshot}}
    if baseline is None:
        occurred=datetime.now(timezone.utc).isoformat()
        raw={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'name':'Fournisseur paiement T1','phone':'+237600000035'}
        status,supplier,_headers=request(prefix,data=raw);assert status==201,(status,supplier)
        profile=entry({**raw,'installation_id':str(installation_id)},'supplier_creation','supplier',supplier,occurred,
            datetime.now(timezone.utc).isoformat(),[]);operations.append(profile);receipts.append(supplier)
        supplier_path=prefix+supplier['id']+'/'
        if documents:
            order=None
            for kind in ('order','invoice'):
                raw={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':datetime.now(timezone.utc).isoformat(),
                    'reason':'Document fournisseur réel T1','external_reference':'DOCUMENT-T1-'+kind,
                    'lines':[{'id':str(uuid.uuid4()),**documents,'quantity_milli':10000,'unit_price_fcfa':10000}]}
                if order is not None:raw['order_id']=order['id']
                status,purchase,_headers=request(supplier_path+('orders/' if kind=='order' else 'invoices/'),data=raw)
                assert status==201 and purchase['total_fcfa']==100000,(status,purchase)
                assert purchase['lines'][0]['factor_milli']==24000 and purchase['lines'][0]['base_quantity_milli']==240000
                assert purchase['lines'][0]['received_quantity_milli']==0 and purchase['lines'][0]['awaiting_quantity_milli']==10000
                dependencies=[profile['operation_id']]+([order_entry['operation_id']] if order is not None else [])
                document=entry({**raw,'supplier_id':supplier['id'],'installation_id':str(installation_id)},'purchase_'+kind,
                    'purchase',{**purchase,'supplier_id':supplier['id']},purchase['occurred_at'],purchase['created_at'],dependencies)
                operations.append(document);receipts.append(purchase)
                if kind=='order':
                    order=purchase;order_entry=document
                    assert purchase['debt'] is None
                    assert request(supplier_path+'debts/')[1]['count']==0
                else:
                    debt=purchase['debt'];invoice_entry=document
                    assert debt['amount_fcfa']==debt['remaining_fcfa']==100000 and debt['document_id']==purchase['id']
                status,cash,_headers=request(f'/api/v1/shops/{shop_id}/cash/')
                assert status==200 and cash['count']==0,(status,cash)
            payment_dependencies=[profile['operation_id']];allocation_dependencies=[invoice_entry['operation_id']]
        else:
            raw={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':datetime.now(timezone.utc).isoformat(),
                'amount_fcfa':100000,'reason':'Dette antérieure réelle T1'}
            status,debt,_headers=request(supplier_path+'debts/',data=raw);assert status==201,(status,debt)
            opening=entry({**raw,'supplier_id':supplier['id'],'installation_id':str(installation_id)},'supplier_opening_debt',
                'opening_debt',{**debt,'supplier_id':supplier['id']},debt['occurred_at'],debt['created_at'],[profile['operation_id']])
            operations.append(opening);receipts.append(debt)
            payment_dependencies=[profile['operation_id']];allocation_dependencies=[opening['operation_id']]
    else:
        supplier={'id':baseline['supplier_id']};debt={'id':baseline['debt_id']};supplier_path=prefix+supplier['id']+'/'
        payment_dependencies=[];allocation_dependencies=[baseline['invoice_operation_id']]
    raw={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':datetime.now(timezone.utc).isoformat(),
        'reason':'Trente mille effectivement versés T1','parts':[
            {'id':str(uuid.uuid4()),'method':'cash','amount_fcfa':10000},
            {'id':str(uuid.uuid4()),'method':'orange','amount_fcfa':20000,'external_reference':'VERSEMENT-T1'}]}
    status,payment,_headers=request(supplier_path+'payments/',data=raw)
    assert status==201 and payment['amount_fcfa']==payment['unallocated_fcfa']==30000 and payment['allocations']==[],(status,payment)
    payment_entry=entry({**raw,'supplier_id':supplier['id'],'installation_id':str(installation_id)},'supplier_payment','payment',payment,
        payment['occurred_at'],payment['created_at'],payment_dependencies);operations.append(payment_entry);receipts.append(payment)
    status,cash,_headers=request(f'/api/v1/shops/{shop_id}/cash/');assert status==200 and cash['count']==2,(status,cash)
    cash_ids={row['id'] for row in cash['results']};assert cash_ids=={str(uuid.uuid5(uuid.UUID(row['id']),'supplier-payment-cash')) for row in payment['parts']}
    raw={'operation_id':str(uuid.uuid4()),'reason':'Trente mille affectés explicitement T1',
        'allocations':[{'debt_id':debt['id'],'amount_fcfa':30000}]}
    status,allocation,_headers=request(supplier_path+f"payments/{payment['id']}/allocate/",data=raw)
    assert status==201 and allocation['allocated_fcfa']==30000 and allocation['after_unallocated_fcfa']==0,(status,allocation)
    frozen={'operation_id':allocation['operation_id'],'supplier_id':supplier['id'],'payment_id':payment['id'],'allocated_fcfa':30000,
        'before_unallocated_fcfa':allocation['after_unallocated_fcfa']+allocation['allocated_fcfa'],'after_unallocated_fcfa':0,
        'recorded_at':allocation['recorded_at'],'allocations':[{key:row[key] for key in ('id','debt_id','amount_fcfa')} for row in allocation['allocations']]}
    operations.append(entry({**raw,'payment_id':payment['id'],'installation_id':str(installation_id)},'supplier_payment_allocation','allocation',
        frozen,allocation['recorded_at'],allocation['recorded_at'],[payment_entry['operation_id'],*allocation_dependencies]))
    receipts.append(allocation)
    # A later allocation changes the current payment but never its first receipt.
    payment_raw={key:value for key,value in payment_entry['payload']['original_request'].items()
        if key not in ('supplier_id','installation_id')}
    assert request(supplier_path+'payments/',data=payment_raw)[:2]==(200,payment)
    assert request(supplier_path+f"payments/{payment['id']}/allocate/",data=raw)[:2]==(200,allocation)
    status,current,_headers=request(supplier_path+f"payments/{payment['id']}/")
    assert status==200 and current['unallocated_fcfa']==0 and len(current['allocations'])==1,(status,current)
    status,cash,_headers=request(f'/api/v1/shops/{shop_id}/cash/')
    assert status==200 and cash['count']==2 and cash['totals_by_method_fcfa']=={'cash':-10000,'orange':-20000},(status,cash)
    assert {row['id'] for row in cash['results']}==cash_ids
    if documents:
        for document in (order_entry,invoice_entry):
            original=document['payload']['original_request']
            raw={key:value for key,value in original.items() if key not in ('supplier_id','installation_id')}
            suffix='orders/' if document['operation_type']=='purchase_order' else 'invoices/'
            expected={key:value for key,value in document['payload']['purchase'].items() if key!='supplier_id'}
            assert request(supplier_path+suffix,data=raw)[:2]==(200,expected)
    return {'supplier_id':supplier['id'],'ordered_operations':operations,'operations':list(reversed(operations)),
        'receipts':receipts,'cash_ids':sorted(cash_ids),'baseline':baseline,'documents':documents}


def recover_supplier_money_flow(request,*,shop_id,foreign_shop_id,session_id,credential,survivor):
    prefix=f'/api/v1/shops/{shop_id}/';supplier_path=prefix+'suppliers/'+survivor['supplier_id']+'/'
    baseline=survivor['baseline'];documents=survivor['documents']
    assert request(supplier_path,credential)[0]==(200 if baseline else 404)
    if baseline:
        status,invoice,_headers=request(supplier_path+f"invoices/{baseline['invoice_id']}/",credential)
        assert status==200 and invoice['debt']['remaining_fcfa']==100000,(status,invoice)
    journal={'operation_id':str(uuid.uuid4()),'format_version':1,'shop_id':str(shop_id),'session_id':session_id,
        'reason':'Quatre faits fournisseur T1 survivants contrôlés','operations':survivor['operations']}
    status,captured,_headers=request(prefix+'recovery/journals/',credential,journal)
    assert status==201 and captured['operation_count']==len(survivor['ordered_operations']) and captured['business_effects_created']==0,(status,captured)
    assert request(prefix+'recovery/journals/',credential,journal)[:2]==(200,captured)
    captures={row['operation_id']:row['capture_id'] for row in captured['operations']}
    def path(operation):return prefix+f'recovery/operations/{captures[operation]}/'
    def preview(operation):
        status,body,_headers=request(path(operation)+'integration-preview/',credential);assert status==200,(status,body)
        return body
    def apply(operation,body):
        command={'operation_id':str(uuid.uuid4()),'previous_version':body['version'],'preview_token':body['preview_token'],
            'reason':'Montants, justificatif et cibles fournisseur contrôlés'}
        status,receipt,_headers=request(path(operation)+'integrate/',credential,command)
        return status,receipt,command
    order=survivor['ordered_operations'];by_type={entry['operation_type']:entry for entry in order}
    payment_entry=by_type['supplier_payment'];allocation_entry=by_type['supplier_payment_allocation']
    allocation_id=allocation_entry['operation_id'];old_allocation=preview(allocation_id)
    assert not old_allocation['can_apply'] and old_allocation['current_state'] is None
    assert request(f"/api/v1/shops/{foreign_shop_id}/recovery/operations/{captures[allocation_id]}/integration-preview/",credential)[0]==404
    first={}
    # Actual payment can recover as an advance before the debt is recovered.
    replay_order=[entry for entry in (by_type.get('supplier_creation'),payment_entry,by_type.get('purchase_order'),
        by_type.get('purchase_invoice'),by_type.get('supplier_opening_debt'),allocation_entry) if entry is not None]
    for entry in replay_order:
        operation=entry['operation_id'];current=preview(operation)
        if entry is payment_entry:assert current['cash_delta_by_method_fcfa']=={'cash':-10000,'orange':-20000}
        if entry is allocation_entry:
            assert apply(operation,old_allocation)[0]==409
            assert current['cash_delta_by_method_fcfa']=={} and current['supplier_debt_delta_fcfa']==current['supplier_advance_delta_fcfa']==-30000
        status,receipt,command=apply(operation,current);assert status==201,(status,receipt)
        first[operation]=receipt['business_receipt']
        assert request(path(operation)+'integrate/',credential,command)[:2]==(200,receipt)
        if entry is payment_entry:
            assert receipt['business_receipt']['unallocated_fcfa']==30000 and receipt['business_receipt']['allocations']==[]
            assert receipt['business_receipt']['installation_id']==payment_entry['origin_installation_id']
            status,debts,_headers=request(supplier_path+'debts/',credential);assert status==200 and debts['count']==(1 if baseline else 0)
            if documents:
                invoice_preview=preview(by_type['purchase_invoice']['operation_id'])
                assert not invoice_preview['can_apply'] and invoice_preview['cash_delta_by_method_fcfa']=={}
        if entry['operation_type'] in ('purchase_order','purchase_invoice'):
            assert current['cash_delta_by_method_fcfa']=={}
            status,cash,_headers=request(prefix+'cash/',credential)
            assert status==200 and cash['count']==2 and cash['totals_by_method_fcfa']=={'cash':-10000,'orange':-20000},(status,cash)
            status,debts,_headers=request(supplier_path+'debts/',credential)
            assert status==200 and debts['count']==(0 if entry['operation_type']=='purchase_order' else 1),(status,debts)
    def without_creation(value):
        if isinstance(value,list):return [without_creation(item) for item in value]
        if isinstance(value,dict):return {key:without_creation(item) for key,item in value.items() if key not in ('created_at','recorded_at')}
        return value
    for entry,original in zip(order,survivor['receipts'],strict=True):
        actual=first[entry['operation_id']];assert without_creation(actual)==without_creation(original)
        current=preview(entry['operation_id']);assert current['outcome']=='already_known' and current['cash_delta_by_method_fcfa']=={}
        status,receipt,_command=apply(entry['operation_id'],current);assert status==201 and receipt['business_receipt']==actual,(status,receipt)
    payment=first[payment_entry['operation_id']]
    # The current reviewer has a different installation from the source. Native
    # retries cannot impersonate the lost session or bypass the reviewed route.
    payment_raw={key:value for key,value in payment_entry['payload']['original_request'].items()
        if key not in ('supplier_id','installation_id')}
    allocation_raw={key:value for key,value in allocation_entry['payload']['original_request'].items()
        if key not in ('payment_id','installation_id')}
    assert request(supplier_path+'payments/',credential,payment_raw)[0]==409
    assert request(supplier_path+f"payments/{payment['id']}/allocate/",credential,allocation_raw)[0]==409
    if documents:
        for kind in ('purchase_order','purchase_invoice'):
            document=by_type[kind];source=document['payload']['purchase']
            suffix='orders/' if kind=='purchase_order' else 'invoices/'
            raw={key:value for key,value in document['payload']['original_request'].items() if key not in ('supplier_id','installation_id')}
            assert request(supplier_path+suffix,credential,raw)[0]==409
            status,detail,_headers=request(supplier_path+suffix+source['id']+'/',credential)
            assert status==200 and detail['lines']==source['lines'] and detail['total_fcfa']==100000,(status,detail)
            assert detail['order_id']==source['order_id']
            if kind=='purchase_invoice':
                assert detail['debt']['id']==source['debt']['id'] and detail['debt']['remaining_fcfa']==70000
                assert request(supplier_path+suffix+source['id']+'/receipts/',credential)[1]['count']==0
    status,detail,_headers=request(supplier_path+f"payments/{payment['id']}/",credential)
    assert status==200 and detail['unallocated_fcfa']==0 and detail['amount_fcfa']==30000 and len(detail['allocations'])==1,(status,detail)
    assert detail['installation_id']==payment_entry['origin_installation_id']
    assert detail['allocations'][0]['id']==allocation_entry['payload']['allocation']['allocations'][0]['id']
    assert detail['allocations'][0]['actor_id']==allocation_entry['origin_user_id']
    status,cash,_headers=request(prefix+'cash/',credential)
    assert status==200 and cash['count']==2 and cash['totals_by_method_fcfa']=={'cash':-10000,'orange':-20000},(status,cash)
    assert {row['id'] for row in cash['results']}==set(survivor['cash_ids'])
    status,statement,_headers=request(supplier_path+'statement/',credential)
    assert status==200 and statement['count']==3 and statement['closing_debt_fcfa']==70000 and statement['closing_advance_fcfa']==0,(status,statement)
    assert statement['closing_credit_fcfa']==0 and statement['closing_balance_fcfa']==70000
    by_operation={entry['operation_id']:entry['origin_user_id'] for entry in order
        if entry['operation_type'] not in ('supplier_creation','purchase_order')}
    if baseline:by_operation[baseline['invoice_operation_id']]=baseline['actor_id']
    assert {row['operation_id']:row['actor_id'] for row in statement['results']}==by_operation
    return {'native_t1_supplier_payment_chain_recovered':len(order),'supplier_advance_recovered_before_debt':not bool(baseline),
        'supplier_invoice_retained_in_t0':bool(baseline),
        'supplier_actual_payment_fcfa':30000,'supplier_exact_allocation_fcfa':30000,'supplier_remaining_debt_fcfa':70000,
        'supplier_remaining_advance_fcfa':0,'supplier_cash_by_method_fcfa':cash['totals_by_method_fcfa'],
        'supplier_cash_uuids_preserved':True,'supplier_source_allocation_uuid_preserved':True,'no_second_supplier_cash_outflow':True,
        'native_supplier_first_receipt_retry_after_allocation':True,'native_reviewer_cannot_impersonate_source_installation':True,
        **({'lost_t1_purchase_order_and_invoice_recovered':2,'purchase_factor_milli':24000,
            'purchase_base_quantity_milli':240000,'purchase_unit_price_fcfa':10000,
            'purchase_documents_create_no_receipt_stock_or_cash':True} if documents else {})}


def audit_supplier_money_t0(db,survivor):
    baseline=survivor['baseline']
    for table,count in (('core_supplier',1 if baseline else 0),('core_supplierdebt',1 if baseline else 0),
            ('core_purchasedocument',1 if baseline else 0),('core_supplierledgerentry',1 if baseline else 0),
            ('core_supplierpayment',0),('core_supplierpaymentallocation',0),('core_cashmovement',0),('core_stockmovement',0)):
        assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==count,table
    if baseline:assert db.execute('SELECT amount_fcfa,remaining_fcfa FROM core_supplierdebt').fetchone()==(100000,100000)


def audit_supplier_money_flow(db,survivor):
    baseline=survivor['baseline'];documents=survivor['documents']
    source_count=len(survivor['ordered_operations'])
    for table,count in (('core_supplier',1),('core_supplierdebt',1),('core_suppliercredit',0),('core_supplierpayment',1),
            ('core_supplierpaymentpart',2),('core_supplierpaymentallocation',1),('core_supplierledgerentry',3),
            ('backups_recoveredoperationorigin',source_count),('backups_recoveryintegrationdecision',source_count*2),('core_stockmovement',0),
            ('core_purchasedocument',1 if baseline else 2 if documents else 0),('core_purchasereceipt',0),
            ('core_purchasereceiptline',0),('core_supplierrefund',0),('core_customer',0)):
        assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==count,table
    assert db.execute('SELECT COUNT(*),SUM(amount_fcfa) FROM core_cashmovement').fetchone()==(2,-30000)
    assert db.execute('SELECT amount_fcfa,remaining_fcfa FROM core_supplierdebt').fetchone()==(100000,70000)
    assert db.execute('SELECT amount_fcfa,unallocated_fcfa FROM core_supplierpayment').fetchone()==(30000,0)
    assert {str(uuid.UUID(row[0])) for row in db.execute('SELECT id FROM core_cashmovement')}==set(survivor['cash_ids'])
    source=next(entry for entry in survivor['ordered_operations'] if entry['operation_type']=='supplier_payment_allocation')
    assert str(uuid.UUID(db.execute('SELECT id FROM core_supplierpaymentallocation').fetchone()[0]))==source['payload']['allocation']['allocations'][0]['id']
    details=json.loads(db.execute('SELECT details FROM core_activity WHERE id=?',[uuid.UUID(source['operation_id']).hex]).fetchone()[0])
    assert datetime.fromisoformat(details['recovery']['source_snapshot']['recorded_at'].replace('Z','+00:00'))==datetime.fromisoformat(source['occurred_at'].replace('Z','+00:00'))
    if documents:
        assert db.execute('SELECT COUNT(*) FROM core_purchaseline').fetchone()[0]==2
        for source in (entry for entry in survivor['ordered_operations'] if entry['operation_type'] in ('purchase_order','purchase_invoice')):
            purchase=source['payload']['purchase'];line=purchase['lines'][0]
            assert db.execute('SELECT quantity_milli,factor_milli,base_quantity_milli,unit_price_fcfa,total_fcfa FROM core_purchaseline WHERE id=?',
                [uuid.UUID(line['id']).hex]).fetchone()==(10000,24000,240000,10000,100000)
            document=db.execute('SELECT kind,order_id,total_fcfa FROM core_purchasedocument WHERE id=?',[uuid.UUID(purchase['id']).hex]).fetchone()
            assert document==(purchase['kind'],uuid.UUID(purchase['order_id']).hex if purchase['order_id'] else None,100000)
            if purchase['kind']=='invoice':
                assert db.execute('SELECT document_id,amount_fcfa,remaining_fcfa FROM core_supplierdebt WHERE id=?',
                    [uuid.UUID(purchase['debt']['id']).hex]).fetchone()==(uuid.UUID(purchase['id']).hex,100000,70000)
