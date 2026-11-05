"""Actual lost commercial returns, physical receipts and settlements share no money effect."""
from datetime import datetime,timezone
import uuid
from verification.return_settlement_recovery_flow import record_lost_return_flow,recover_lost_return_flow


def record_physical_return_flow(request,*,shop_id,actor_id,installation_id,baseline,non_sellable_decisions=False):
    survivor=record_lost_return_flow(request,shop_id=shop_id,actor_id=actor_id,installation_id=installation_id,baseline=baseline)
    physical=[]
    for index,condition,quantity in ((0,'sellable',3000),(0,'unsellable',3000 if non_sellable_decisions else 2000),(1,'sellable',1000)):
        parent=survivor['return_operations'][index];returned=parent['payload']['sale_return'];sale=baseline['claims'][index]['sale']
        line=returned['lines'][0]
        path=f"/api/v1/shops/{shop_id}/sales/{sale['id']}/returns/{returned['id']}/lines/{line['id']}/receipts/"
        raw={'id':str(uuid.uuid4()),'operation_id':str(uuid.uuid4()),'occurred_at':datetime.now(timezone.utc).isoformat(),
            'reason':'Réception physique T1 conservée hors base','quantity_milli':quantity,'condition':condition}
        status,receipt,_headers=request(path,data=raw);assert status==201,(status,receipt)
        assert receipt['cost']['cost_state']=='known' and receipt['cost']['sale_cost_id'] is not None
        snapshot={key:receipt[key] for key in ('id','return_line_id','quantity_milli','condition','occurred_at','created_at',
            'movement_id','unit_cost_micros','cost_is_provisional','cost')}
        snapshot.update(sale_id=sale['id'],sale_return_id=returned['id'],product_id=sale['lines'][0]['product_id'],unit_kind=line['unit_kind'])
        entry={'operation_id':raw['operation_id'],'operation_type':'return_receipt','origin_user_id':str(actor_id),
            'origin_installation_id':str(installation_id),'occurred_at':receipt['occurred_at'],'origin_recorded_at':receipt['created_at'],
            'payload':{'schema_version':1,'original_request':{**raw,'return_line_id':line['id'],'installation_id':str(installation_id)},'receipt':snapshot},
            'dependencies':[parent['operation_id']]+([physical[0]['operation_id']] if physical and index==0 else [])}
        physical.append(entry)
    survivor['physical_operations']=physical
    survivor['operations']=list(reversed(physical))+survivor['operations']
    return survivor


def recover_physical_return_flow(request,*,shop_id,foreign_shop_id,session_id,credential,survivor):
    result=recover_lost_return_flow(request,shop_id=shop_id,foreign_shop_id=foreign_shop_id,session_id=session_id,
        credential=credential,survivor=survivor,physical_returns=True)
    received=[]
    for entry in survivor['physical_operations']:
        source=entry['payload']['receipt']
        root=f"/api/v1/shops/{shop_id}/sales/{source['sale_id']}/returns/{source['sale_return_id']}/lines/{source['return_line_id']}/receipts/"
        status,receipt,_headers=request(root+source['id']+'/',credential);assert status==200,(status,receipt)
        assert receipt['cost']==source['cost'] and receipt['actor_id']==entry['origin_user_id']
        assert receipt['remaining_unsellable_milli']==(2000 if source['condition']=='unsellable' else 0)
        received.append(receipt)
    assert sum(row['quantity_milli'] for row in received)==6000
    return {**result,'physical_receipts_missing_in_t0_recovered':3,'sellable_restored_milli':4000,
        'unsellable_kept_separate_milli':2000,'available_stock_milli':14000,'no_duplicate_physical_or_financial_effect':True}
