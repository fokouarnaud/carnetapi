"""T0 holds real return claims; T1 settlements survive outside the restored database."""
from datetime import datetime,timezone
import uuid


def prepare_return_baseline(*,shop,owner,installation,session,product,lost_returns=False):
    """Use native REST views before the archive, with a fixture stock opening."""
    from rest_framework.test import APIClient
    from rest_framework_simplejwt.tokens import AccessToken
    from common.models import Activity
    from stock.models import StockMovement
    token=AccessToken.for_user(owner);token["session_id"]=str(session.id)
    api=APIClient();api.credentials(HTTP_AUTHORIZATION="Bearer "+str(token))
    def post(path,data):
        response=api.post(path,data,format="json");assert response.status_code==201,(response.status_code,response.content)
        return response.data
    prefix=f"/api/v1/shops/{shop.id}/"
    customer=post(prefix+"clients/",{"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"name":"Client avec avoir T0"})
    debt=post(prefix+f"clients/{customer['id']}/debts/",{"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),
        "occurred_at":datetime.now(timezone.utc).isoformat(),"amount_fcfa":8000,"reason":"Dette d'ouverture T0"})
    opening=Activity.objects.create(shop=shop,actor=owner,action="opening_stock",target_id=product.id,reason="Stock de qualification T0")
    StockMovement.objects.create(product=product,actor=owner,operation=opening,kind="opening",quantity_milli=30000,
        unit_cost_micros=100000000,occurred_at=datetime.now(timezone.utc),reason="Stock physique de la fixture")
    claims=[]
    for identified in (True,False):
        sale=post(prefix+"sales/",{"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),
            "customer_id":customer["id"] if identified else None,"lines":[{"id":str(uuid.uuid4()),"product_id":str(product.id),
                "product_version":product.version,"quantity_milli":10000}],"payments":[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":5000}]})
        if lost_returns:
            claims.append({"sale_id":sale["id"],"customer_id":customer["id"] if identified else None,"sale":sale})
            continue
        returned=post(prefix+f"sales/{sale['id']}/returns/",{"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),
            "occurred_at":datetime.now(timezone.utc).isoformat(),"reason":"Retour commercial T0 sans réception physique",
            "lines":[{"id":str(uuid.uuid4()),"sale_line_id":sale["lines"][0]["id"],"quantity_milli":10000}]})
        assert returned["credit_fcfa"]==5000
        claims.append({"sale_id":sale["id"],"sale_return_id":returned["id"],"customer_id":customer["id"] if identified else None})
    return {"customer":customer,"debt":debt,"claims":claims}


def record_return_flow(request,*,shop_id,actor_id,installation_id,baseline):
    prefix=f"/api/v1/shops/{shop_id}/";entries=[];receipts=[]
    for index,kind,amount in ((0,"refund",1000),(0,"allocation",2000),(1,"refund",500)):
        claim=baseline["claims"][index];base=prefix+f"sales/{claim['sale_id']}/returns/{claim['sale_return_id']}/"
        status,current,_headers=request(base);assert status==200,(status,current)
        raw={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"occurred_at":datetime.now(timezone.utc).isoformat(),
            "reason":"Règlement T1 réel conservé après restauration"}
        if kind=="refund":raw.update(amount_fcfa=amount,method="cash")
        else:raw["allocations"]=[{"id":str(uuid.uuid4()),"debt_id":baseline["debt"]["id"],"amount_fcfa":amount}]
        status,receipt,_headers=request(base+("refunds/" if kind=="refund" else "allocations/"),data=raw)
        assert status==201,(status,receipt)
        snapshot={key:receipt[key] for key in ("id","sale_return_id","kind","amount_fcfa","after_credit_fcfa","occurred_at","created_at","method","other_name","external_reference","allocations")}
        snapshot.update(sale_id=claim["sale_id"],customer_id=claim["customer_id"],before_credit_fcfa=current["remaining_credit_fcfa"])
        entry={"operation_id":raw["operation_id"],"operation_type":"return_credit_settlement","origin_user_id":str(actor_id),
            "origin_installation_id":str(installation_id),"occurred_at":receipt["occurred_at"],"origin_recorded_at":receipt["created_at"],
            "payload":{"schema_version":1,"original_request":{**raw,"sale_return_id":claim["sale_return_id"],"kind":kind,"installation_id":str(installation_id)},
                "settlement":snapshot},"dependencies":[entries[0]["operation_id"]] if kind=="allocation" else []}
        entries.append(entry);receipts.append(receipt)
    return {"baseline":baseline,"operations":[entries[1],entries[2],entries[0]],"original_order":[entry["operation_id"] for entry in entries],"receipts":receipts}


def recover_return_flow(request,*,shop_id,foreign_shop_id,session_id,credential,survivor):
    prefix=f"/api/v1/shops/{shop_id}/";baseline=survivor["baseline"]
    for claim in baseline["claims"]:
        base=prefix+f"sales/{claim['sale_id']}/returns/{claim['sale_return_id']}/"
        status,current,_headers=request(base,credential);assert status==200 and current["remaining_credit_fcfa"]==5000,(status,current)
        for kind in ("refunds/","allocations/"):
            status,rows,_headers=request(base+kind,credential);assert status==200 and rows["count"]==0,(status,rows)
    journal={"operation_id":str(uuid.uuid4()),"format_version":1,"shop_id":str(shop_id),"session_id":session_id,
        "reason":"Règlements T1 survivants contrôlés","operations":survivor["operations"]}
    status,captured,_headers=request(prefix+"recovery/journals/",credential,journal)
    assert status==201 and captured["operation_count"]==len(survivor["operations"]) and captured["business_effects_created"]==0,(status,captured)
    captures={row["operation_id"]:row["capture_id"] for row in captured["operations"]}
    def path(operation):return prefix+f"recovery/operations/{captures[operation]}/"
    def preview(operation):
        status,body,_headers=request(path(operation)+"integration-preview/",credential);assert status==200,(status,body)
        return body
    def apply(operation,body):
        command={"operation_id":str(uuid.uuid4()),"previous_version":body["version"],"preview_token":body["preview_token"],"reason":"Preuves et solde du retour contrôlés"}
        status,receipt,_headers=request(path(operation)+"integrate/",credential,command)
        return status,receipt,command
    old_allocation=preview(survivor["original_order"][1]);assert not old_allocation["can_apply"]
    any_capture=next(iter(captures.values()))
    assert request(f"/api/v1/shops/{foreign_shop_id}/recovery/operations/{any_capture}/integration-preview/",credential)[0]==404
    for index,operation in enumerate(survivor["original_order"]):
        if index==1:
            status,body,_command=apply(operation,old_allocation);assert status==409,(status,body)
        status,receipt,command=apply(operation,preview(operation));assert status==201,(status,receipt)
        old=survivor["receipts"][index];actual=receipt["business_receipt"]
        assert {key:value for key,value in actual.items() if key!="created_at"}=={key:value for key,value in old.items() if key!="created_at"}
        assert request(path(operation)+"integrate/",credential,command)[:2]==(200,receipt)
        known=preview(operation);assert known["outcome"]=="already_known" and known["cash_delta_by_method_fcfa"]=={}
        status,ack,_command=apply(operation,known);assert status==201 and ack["business_receipt"]==actual,(status,ack)
    for claim,expected in zip(baseline["claims"],(2000,4500),strict=True):
        base=prefix+f"sales/{claim['sale_id']}/returns/{claim['sale_return_id']}/"
        status,current,_headers=request(base,credential);assert status==200 and current["remaining_credit_fcfa"]==expected,(status,current)
    status,cash,_headers=request(prefix+"cash/",credential);assert status==200 and cash["count"]==4
    assert sum(row["amount_fcfa"] for row in cash["results"])==8500
    status,debts,_headers=request(prefix+f"clients/{baseline['customer']['id']}/debts/",credential)
    assert status==200 and debts["count"]==1 and debts["results"][0]["remaining_fcfa"]==6000
    return {"return_claims_already_in_t0":True,"native_t1_return_settlements_recovered":3,
        "identified_return_credit_fcfa":2000,"anonymous_return_credit_fcfa":4500,"remaining_debt_fcfa":6000,
        "return_flow_net_cash_fcfa":8500,"no_second_return_or_physical_receipt":True,"no_anonymous_customer_created":True}


def audit_return_t0(db,baseline,*,lost_returns=False):
    for table,count in (("core_customer",1),("core_sale",2),("core_salepayment",2),("core_salereturn",0 if lost_returns else 2),
            ("core_returncreditsettlement",0),("core_returncreditallocation",0),("core_customerledgerentry",3 if lost_returns else 4),
            ("core_stockmovement",3),("core_returnreceipt",0),("core_customerpayment",0)):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]==count,table
    assert db.execute("SELECT COUNT(*),SUM(amount_fcfa) FROM core_cashmovement").fetchone()==(2,10000)
    assert db.execute("SELECT remaining_fcfa FROM core_customerdebt WHERE id=?",[uuid.UUID(baseline["debt"]["id"]).hex]).fetchone()[0]==8000
    assert db.execute("SELECT COUNT(*) FROM core_customer WHERE id=?",[uuid.UUID(baseline["customer"]["id"]).hex]).fetchone()[0]==1


def audit_return_flow(db,baseline,*,lost_returns=False,physical_returns=False,non_sellable_decisions=False,sale_cancellations=False):
    for table,count in (("core_customer",1),("core_sale",2),("core_salepayment",2),("core_salereturn",2),
            ("core_returncreditsettlement",3),("core_returncreditallocation",1),("core_customerledgerentry",6),
            ("backups_recoveredoperationorigin",7 if sale_cancellations else 11 if non_sellable_decisions else 8 if physical_returns else 5 if lost_returns else 3),
            ("backups_recoveryintegrationdecision",16 if sale_cancellations else 22 if non_sellable_decisions else 16 if physical_returns else 10 if lost_returns else 6),
            ("core_stockmovement",6 if non_sellable_decisions else 5 if physical_returns else 3),("core_returnreceipt",3 if physical_returns else 0),
            ("core_returncostsnapshot",3 if physical_returns else 0),("core_customerpayment",0)):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]==count,table
    assert db.execute('SELECT COUNT(*) FROM core_salecancellation').fetchone()[0]==(2 if sale_cancellations else 0)
    assert db.execute("SELECT COUNT(*),SUM(amount_fcfa) FROM core_cashmovement").fetchone()==(4,8500)
    assert db.execute("SELECT COUNT(*),SUM(remaining_fcfa) FROM core_customerdebt").fetchone()==(1,6000)
    assert db.execute("SELECT SUM(quantity_milli) FROM core_stockmovement").fetchone()[0]==(15000 if non_sellable_decisions else 14000 if physical_returns else 10000)
    if physical_returns:
        assert db.execute("SELECT SUM(quantity_milli) FROM core_returnreceipt WHERE condition='unsellable'").fetchone()[0]==(3000 if non_sellable_decisions else 2000)
        assert db.execute("SELECT SUM(CAST(total_cost_nanos_fcfa AS INTEGER)) FROM core_returncostsnapshot").fetchone()[0]==(700000000000 if non_sellable_decisions else 600000000000)
    assert db.execute('SELECT COUNT(*) FROM core_nonsellabledecision').fetchone()[0]==(3 if non_sellable_decisions else 0)
    assert db.execute('SELECT COUNT(*) FROM core_supplierledgerentry').fetchone()[0]==(1 if non_sellable_decisions else 0)
    if non_sellable_decisions:
        assert db.execute('SELECT SUM(quantity_milli) FROM core_nonsellabledecision').fetchone()[0]==3000
        assert db.execute('SELECT delta_fcfa,debt_delta_fcfa,advance_delta_fcfa,credit_delta_fcfa FROM core_supplierledgerentry').fetchone()==(0,0,0,0)
        for table in ('core_supplierdebt','core_suppliercredit','core_supplierreturn','core_stockloss'):
            assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==0,table


def record_lost_return_flow(request,*,shop_id,actor_id,installation_id,baseline):
    returns=[]
    for claim in baseline["claims"]:
        sale=claim["sale"]
        raw={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"occurred_at":datetime.now(timezone.utc).isoformat(),
            "reason":"Retour commercial T1 réellement accepté","lines":[{"id":str(uuid.uuid4()),"sale_line_id":sale["lines"][0]["id"],"quantity_milli":10000}]}
        status,receipt,_headers=request(f"/api/v1/shops/{shop_id}/sales/{sale['id']}/returns/",data=raw)
        assert status==201 and receipt["amount_fcfa"]==5000,(status,receipt)
        snapshot={key:receipt[key] for key in ("id","sale_id","amount_fcfa","debt_reduced_fcfa","credit_fcfa","occurred_at","created_at","lines")}
        snapshot["customer_id"]=claim["customer_id"];claim["sale_return_id"]=receipt["id"]
        returns.append({"operation_id":raw["operation_id"],"operation_type":"sale_return","origin_user_id":str(actor_id),
            "origin_installation_id":str(installation_id),"occurred_at":receipt["occurred_at"],"origin_recorded_at":receipt["created_at"],
            "payload":{"schema_version":1,"original_request":{**raw,"sale_id":sale["id"],"installation_id":str(installation_id)},"sale_return":snapshot},"dependencies":[]})
    survivor=record_return_flow(request,shop_id=shop_id,actor_id=actor_id,installation_id=installation_id,baseline=baseline)
    by_return={entry["payload"]["sale_return"]["id"]:entry["operation_id"] for entry in returns}
    for entry in survivor["operations"]:entry["dependencies"].append(by_return[entry["payload"]["settlement"]["sale_return_id"]])
    survivor["operations"].extend(reversed(returns));survivor["return_operations"]=returns
    return survivor


def recover_lost_return_flow(request,*,shop_id,foreign_shop_id,session_id,credential,survivor,physical_returns=False,non_sellable_decisions=False,sale_cancellations=False):
    prefix=f"/api/v1/shops/{shop_id}/";baseline=survivor["baseline"]
    for claim in baseline["claims"]:
        status,rows,_headers=request(prefix+f"sales/{claim['sale_id']}/returns/",credential)
        assert status==200 and rows["count"]==0,(status,rows)
    journal={"operation_id":str(uuid.uuid4()),"format_version":1,"shop_id":str(shop_id),"session_id":session_id,
        "reason":"Retours et règlements T1 survivants contrôlés","operations":survivor["operations"]}
    status,captured,_headers=request(prefix+"recovery/journals/",credential,journal)
    assert status==201 and captured["operation_count"]==(7 if sale_cancellations else 11 if non_sellable_decisions else 8 if physical_returns else 5) and captured["business_effects_created"]==0,(status,captured)
    captures={row["operation_id"]:row["capture_id"] for row in captured["operations"]}
    def path(operation):return prefix+f"recovery/operations/{captures[operation]}/"
    def preview(operation):
        status,body,_headers=request(path(operation)+"integration-preview/",credential);assert status==200,(status,body)
        return body
    def apply(operation,body):
        command={"operation_id":str(uuid.uuid4()),"previous_version":body["version"],"preview_token":body["preview_token"],"reason":"Retour et règlement d'origine contrôlés"}
        status,receipt,_headers=request(path(operation)+"integrate/",credential,command)
        return status,receipt,command
    old_refund=preview(survivor["original_order"][0]);assert not old_refund["can_apply"]
    for entry in survivor["cancellation_operations"] if sale_cancellations else survivor["return_operations"]:
        operation=entry["operation_id"];status,receipt,command=apply(operation,preview(operation));assert status==201,(status,receipt)
        business=receipt["business_receipt"]
        if sale_cancellations:
            source=entry['payload']['cancellation'];assert len(receipt['related_integration_decision_ids'])==1
            assert (business['id'],business['sale_id'],business['sale_return_id'],business['actor_id'])==(source['id'],source['sale_id'],source['sale_return_id'],entry['origin_user_id'])
            status,current,_headers=request(prefix+f"sales/{source['sale_id']}/cancellation/",credential)
            assert status==200 and current==business,(status,current)
            status,child,_headers=request(prefix+f"sales/{source['sale_id']}/returns/{source['sale_return_id']}/",credential)
            assert status==200 and (child['amount_fcfa'],child['credit_fcfa'],child['debt_reduced_fcfa'],child['actor_id'])==(5000,5000,0,entry['origin_user_id']),(status,child)
        else:
            source=entry["payload"]["sale_return"]
            assert (business["id"],business["amount_fcfa"],business["credit_fcfa"],business["debt_reduced_fcfa"])==(source["id"],5000,5000,0)
        assert request(path(operation)+"integrate/",credential,command)[:2]==(200,receipt)
    status,body,_command=apply(survivor["original_order"][0],old_refund);assert status==409,(status,body)
    old_allocation=preview(survivor["original_order"][1]);assert not old_allocation["can_apply"]
    first_return=survivor["return_operations"][0]["operation_id"]
    assert request(f"/api/v1/shops/{foreign_shop_id}/recovery/operations/{captures[first_return]}/integration-preview/",credential)[0]==404
    for index,operation in enumerate(survivor["original_order"]):
        if index==1:
            status,body,_command=apply(operation,old_allocation);assert status==409,(status,body)
        status,receipt,command=apply(operation,preview(operation));assert status==201,(status,receipt)
        assert request(path(operation)+"integrate/",credential,command)[:2]==(200,receipt)
    if physical_returns:
        old_second=preview(survivor['physical_operations'][1]['operation_id']);assert not old_second['can_apply']
        for index,entry in enumerate(survivor['physical_operations']):
            operation=entry['operation_id']
            if index==1:
                status,body,_command=apply(operation,old_second);assert status==409,(status,body)
            current=preview(operation);assert current['cash_delta_by_method_fcfa']=={}
            delta=sum(int(row['validated_after_milli'])-int(row['validated_before_milli']) for row in current['stocks'])
            assert delta==(entry['payload']['receipt']['quantity_milli'] if entry['payload']['receipt']['condition']=='sellable' else 0)
            status,receipt,command=apply(operation,current);assert status==201,(status,receipt)
            business=receipt['business_receipt'];source=entry['payload']['receipt']
            assert business['id']==source['id'] and business['cost']==source['cost'] and business['movement_id']==source['movement_id']
            assert request(path(operation)+'integrate/',credential,command)[:2]==(200,receipt)
    if non_sellable_decisions:
        old_second=preview(survivor['disposition_operations'][1]['operation_id']);assert not old_second['can_apply']
        for index,entry in enumerate(survivor['disposition_operations']):
            operation=entry['operation_id'];source=entry['payload']['decision']
            if index==1:
                status,body,_command=apply(operation,old_second);assert status==409,(status,body)
            current=preview(operation);assert current['cash_delta_by_method_fcfa']=={}
            delta=sum(int(row['validated_after_milli'])-int(row['validated_before_milli']) for row in current['stocks'])
            assert delta==(source['quantity_milli'] if source['kind']=='restock' else 0)
            status,receipt,command=apply(operation,current);assert status==201,(status,receipt)
            business=receipt['business_receipt']
            assert business['id']==source['id'] and business['cost']==source['cost'] and business['after_quantity_milli']==source['after_quantity_milli']
            assert request(path(operation)+'integrate/',credential,command)[:2]==(200,receipt)
    for entry in survivor["operations"]:
        operation=entry["operation_id"];known=preview(operation);assert known["outcome"]=="already_known" and known["cash_delta_by_method_fcfa"]=={},known
        status,receipt,_command=apply(operation,known);assert status==201 and receipt["outcome"]=="already_known",(status,receipt)
    for claim,expected in zip(baseline["claims"],(2000,4500),strict=True):
        status,body,_headers=request(prefix+f"sales/{claim['sale_id']}/returns/{claim['sale_return_id']}/",credential)
        assert status==200 and body["remaining_credit_fcfa"]==expected,(status,body)
    status,cash,_headers=request(prefix+"cash/",credential);assert status==200 and cash["count"]==4
    assert sum(row["amount_fcfa"] for row in cash["results"])==8500
    result={"return_claims_missing_in_t0_recovered":2,"native_t1_settlements_recovered":3,"return_flow_net_cash_fcfa":8500,
        "identified_return_credit_fcfa":2000,"anonymous_return_credit_fcfa":4500}
    if sale_cancellations:result.update(native_t1_cancellations_recovered=2,compensating_returns_linked_atomically=True,
        cancellation_recovery_origins=7,cancellation_recovery_decisions=16)
    if not physical_returns:result['no_second_return_or_physical_receipt']=True
    return result


def record_cancellation_flow(request,*,shop_id,actor_id,installation_id,baseline):
    """Preserve both actual native activities of each cancellation outside T0."""
    cancellations=[];returns=[]
    for claim in baseline['claims']:
        sale=claim['sale'];cancel_id=uuid.uuid4()
        raw={'id':str(cancel_id),'operation_id':str(uuid.uuid4()),'occurred_at':datetime.now(timezone.utc).isoformat(),
            'reason':'Annulation T1 réellement confirmée'}
        prefix=f"/api/v1/shops/{shop_id}/sales/{sale['id']}/"
        status,cancellation,_headers=request(prefix+'cancellation/',data=raw);assert status==201,(status,cancellation)
        status,returned,_headers=request(prefix+f"returns/{cancellation['sale_return_id']}/");assert status==200,(status,returned)
        snapshot={key:returned[key] for key in ('id','sale_id','amount_fcfa','debt_reduced_fcfa','credit_fcfa','occurred_at','created_at','lines')}
        snapshot['customer_id']=claim['customer_id'];claim['sale_return_id']=returned['id']
        child_request={'id':returned['id'],'operation_id':str(uuid.uuid5(cancel_id,'cancellation-return-operation')),
            'occurred_at':raw['occurred_at'],'reason':raw['reason'],'sale_id':sale['id'],'installation_id':str(installation_id),
            'lines':[{key:row[key] for key in ('id','sale_line_id','quantity_milli')} for row in returned['lines']]}
        child_payload={'schema_version':1,'original_request':child_request,'sale_return':snapshot}
        common={'origin_user_id':str(actor_id),'origin_installation_id':str(installation_id),'occurred_at':cancellation['occurred_at']}
        child={**common,'operation_id':child_request['operation_id'],'operation_type':'sale_return',
            'origin_recorded_at':returned['created_at'],'payload':child_payload,'dependencies':[]}
        parent={**common,'operation_id':raw['operation_id'],'operation_type':'sale_cancellation',
            'origin_recorded_at':cancellation['created_at'],'dependencies':[child['operation_id']],
            'payload':{'schema_version':1,'original_request':{**raw,'sale_id':sale['id'],'installation_id':str(installation_id)},
                'cancellation':{**{key:cancellation[key] for key in ('id','sale_id','sale_return_id','occurred_at','created_at')},
                    'compensating_return':child_payload}}}
        returns.append(child);cancellations.append(parent)
    survivor=record_return_flow(request,shop_id=shop_id,actor_id=actor_id,installation_id=installation_id,baseline=baseline)
    by_return={entry['payload']['sale_return']['id']:entry['operation_id'] for entry in returns}
    for entry in survivor['operations']:entry['dependencies'].append(by_return[entry['payload']['settlement']['sale_return_id']])
    survivor['operations'].extend([*cancellations,*reversed(returns)])
    survivor.update(return_operations=returns,cancellation_operations=cancellations)
    return survivor


def recover_cancellation_flow(request,**kwargs):
    return recover_lost_return_flow(request,**kwargs,sale_cancellations=True)
