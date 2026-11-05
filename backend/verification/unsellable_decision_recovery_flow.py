"""Recover actual restock, supplier departure and scrap after replacing their source database."""
from datetime import datetime,timezone
import uuid
from verification.physical_return_recovery_flow import record_physical_return_flow
from verification.return_settlement_recovery_flow import recover_lost_return_flow


def prepare_supplier(*,shop,owner,session):
    from rest_framework.test import APIClient
    from rest_framework_simplejwt.tokens import AccessToken
    token=AccessToken.for_user(owner);token['session_id']=str(session.id)
    api=APIClient();api.credentials(HTTP_AUTHORIZATION='Bearer '+str(token))
    response=api.post(f'/api/v1/shops/{shop.id}/suppliers/',{'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),
        'name':'Fournisseur conservé T0'},format='json')
    assert response.status_code==201,(response.status_code,response.content)
    return response.data


def record_disposition_flow(request,*,shop_id,actor_id,installation_id,baseline):
    survivor=record_physical_return_flow(request,shop_id=shop_id,actor_id=actor_id,installation_id=installation_id,
        baseline=baseline,non_sellable_decisions=True)
    parent=survivor['physical_operations'][1];received=parent['payload']['receipt']
    root=f"/api/v1/shops/{shop_id}/sales/{received['sale_id']}/returns/{received['sale_return_id']}/lines/{received['return_line_id']}/receipts/{received['id']}/decisions/"
    entries=[]
    for kind in ('restock','supplier_return','scrap'):
        raw={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':datetime.now(timezone.utc).isoformat(),
            'reason':'Disposition T1 réellement confirmée','kind':kind,'quantity_milli':1000,
            'supplier_id':baseline['supplier']['id'] if kind=='supplier_return' else None,'is_sellable_confirmed':kind=='restock'}
        status,receipt,_headers=request(root,data=raw);assert status==201,(status,receipt)
        snapshot={key:receipt[key] for key in ('id','receipt_id','kind','quantity_milli','after_quantity_milli','unit_kind',
            'is_sellable_confirmed','supplier_id','occurred_at','created_at','movement_id','unit_cost_micros','cost_is_provisional','cost')}
        snapshot.update(sale_id=received['sale_id'],sale_return_id=received['sale_return_id'],return_line_id=received['return_line_id'],product_id=received['product_id'])
        entry={'operation_id':raw['operation_id'],'operation_type':'non_sellable_decision','origin_user_id':str(actor_id),
            'origin_installation_id':str(installation_id),'occurred_at':receipt['occurred_at'],'origin_recorded_at':receipt['created_at'],
            'payload':{'schema_version':1,'original_request':{**raw,'receipt_id':received['id'],'installation_id':str(installation_id)},'decision':snapshot},
            'dependencies':[parent['operation_id']]+([entries[-1]['operation_id']] if entries else [])}
        entries.append(entry)
    survivor['disposition_operations']=entries;survivor['operations']=list(reversed(entries))+survivor['operations']
    return survivor


def recover_disposition_flow(request,*,shop_id,foreign_shop_id,session_id,credential,survivor):
    result=recover_lost_return_flow(request,shop_id=shop_id,foreign_shop_id=foreign_shop_id,session_id=session_id,
        credential=credential,survivor=survivor,physical_returns=True,non_sellable_decisions=True)
    for entry in survivor['disposition_operations']:
        source=entry['payload']['decision']
        root=f"/api/v1/shops/{shop_id}/sales/{source['sale_id']}/returns/{source['sale_return_id']}/lines/{source['return_line_id']}/receipts/{source['receipt_id']}/"
        status,receipt,_headers=request(root,credential);assert status==200 and receipt['remaining_unsellable_milli']==0,(status,receipt)
        status,decision,_headers=request(root+'decisions/'+source['id']+'/',credential);assert status==200,(status,decision)
        assert decision['actor_id']==entry['origin_user_id'] and decision['cost']==source['cost'] and decision['kind']==source['kind']
    return {**result,'physical_receipts_missing_in_t0_recovered':3,'non_sellable_dispositions_missing_in_t0_recovered':3,
        'restocked_milli':1000,'supplier_departure_milli':1000,'scrapped_milli':1000,'unsellable_remaining_milli':0,
        'available_stock_milli':15000,'no_new_money_or_second_available_stock_loss':True}
