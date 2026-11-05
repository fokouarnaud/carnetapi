"""Keep T1 HTTP receipts outside the database restored to T0, then recover them."""
from datetime import datetime,timedelta,timezone as utc
import uuid


def record_customer_flow(request,*,shop_id,actor_id,installation_id):
    """Record genuine native HTTP operations after the archive was captured."""
    prefix=f"/api/v1/shops/{shop_id}/clients/"
    customer_request={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"name":"Client T1 retrouvé","phone":"+237600000021"}
    status,customer,_headers=request(prefix,data=customer_request)
    assert status==201,(status,customer)
    customer_path=prefix+customer["id"]+"/"
    opening_request={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"amount_fcfa":8000,
        "occurred_at":datetime.now(utc.utc).isoformat(),"due_date":"2026-11-02","reason":"Dette antérieure réellement reprise en T1"}
    status,opening,_headers=request(customer_path+"debts/",data=opening_request)
    assert status==201,(status,opening)
    payment_request={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),
        "parts":[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":3000,"received_fcfa":6000,"returned_fcfa":3000}],
        "allocations":[{"id":str(uuid.uuid4()),"debt_id":opening["id"],"amount_fcfa":2000}]}
    status,payment,_headers=request(customer_path+"payments/",data=payment_request)
    assert status==201 and payment["amount_fcfa"]==3000 and payment["unallocated_fcfa"]==1000,(status,payment)
    status,debts,_headers=request(customer_path+"debts/")
    assert status==200 and debts["results"][0]["remaining_fcfa"]==6000
    contact_request={"operation_id":str(uuid.uuid4()),"base_version":1,"name":"Client T1 corrigé",
        "reason":"Identité corrigée explicitement en T1"}
    status,corrected,_headers=request(customer_path,data=contact_request,method="PATCH")
    assert status==200 and corrected["version"]==2,(status,corrected)
    deadline_request={"operation_id":str(uuid.uuid4()),"base_version":1,"due_date":"2026-11-04",
        "reason":"Report de l'échéance accepté en T1"}
    debt_path=customer_path+"debts/"+opening["id"]+"/"
    status,rescheduled,_headers=request(debt_path+"due-date/",data=deadline_request)
    assert status==200 and rescheduled["due_version"]==2 and rescheduled["remaining_fcfa"]==6000,(status,rescheduled)
    status,history,_headers=request(debt_path+"history/")
    assert status==200,(status,history)
    deadline_time=next(row["created_at"] for row in history["results"] if row["id"]==deadline_request["operation_id"])
    refund_request={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"amount_fcfa":500,
        "method":"cash","reason":"Argent effectivement rendu en T1"}
    status,refund,_headers=request(customer_path+"payments/"+payment["id"]+"/refund/",data=refund_request)
    assert status==201 and refund["after_unallocated_fcfa"]==500,(status,refund)
    allocation_request={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"reason":"Affectation explicite du crédit restant en T1",
        "allocations":[{"id":str(uuid.uuid4()),"debt_id":opening["id"],"amount_fcfa":150},
            {"id":str(uuid.uuid4()),"debt_id":opening["id"],"amount_fcfa":250}]}
    status,allocation,_headers=request(customer_path+"payments/"+payment["id"]+"/allocate/",data=allocation_request)
    assert status==201 and allocation["amount_fcfa"]==400 and allocation["after_unallocated_fcfa"]==100,(status,allocation)
    recorded=datetime.now(utc.utc)+timedelta(seconds=1)
    def entry(kind,operation_id,occurred,payload,dependencies):
        return {"operation_type":kind,"operation_id":operation_id,"origin_user_id":str(actor_id),
            "origin_installation_id":str(installation_id),"occurred_at":occurred,"origin_recorded_at":recorded.isoformat(),
            "payload":payload,"dependencies":dependencies}
    customer_snapshot={key:customer[key] for key in ("id","name","phone","version","created_at","updated_at")}
    opening_snapshot={key:opening[key] for key in ("id","customer_id","sale_id","amount_fcfa","remaining_fcfa","occurred_at","due_at","due_version")}
    collection_snapshot={key:payment[key] for key in ("id","customer_id","amount_fcfa","unallocated_fcfa","requires_regularization")}
    collection_snapshot["parts"]=[{key:row[key] for key in ("id","method","amount_fcfa","other_name","external_reference","received_fcfa","returned_fcfa")}
        for row in payment["parts"]]
    collection_snapshot["allocations"]=[{key:row[key] for key in ("id","debt_id","requested_amount_fcfa","applied_amount_fcfa")}
        for row in payment["allocations"]]
    customer_entry=entry("customer_creation",customer_request["operation_id"],customer["created_at"],
        {"schema_version":1,"original_request":customer_request,"customer":customer_snapshot},[])
    opening_entry=entry("customer_opening_debt",opening_request["operation_id"],opening["occurred_at"],
        {"schema_version":1,"original_request":{**opening_request,"customer_id":customer["id"],"installation_id":str(installation_id)},
            "opening_debt":opening_snapshot},[customer_request["operation_id"]])
    collection_entry=entry("customer_collection",payment_request["operation_id"],payment["occurred_at"],
        {"schema_version":1,"original_request":{**payment_request,"customer_id":customer["id"],"installation_id":str(installation_id)},
            "collection":collection_snapshot},[customer_request["operation_id"],opening_request["operation_id"]])
    corrected_snapshot={key:corrected[key] for key in customer_snapshot}
    contact_entry=entry("customer_update",contact_request["operation_id"],corrected["updated_at"],
        {"schema_version":1,"original_request":{**contact_request,"client_id":customer["id"]},
            "customer_update":{"before":customer_snapshot,"after":corrected_snapshot}},[customer_request["operation_id"]])
    deadline_entry=entry("customer_due_change",deadline_request["operation_id"],deadline_time,
        {"schema_version":1,"original_request":{**deadline_request,"debt_id":opening["id"],"installation_id":str(installation_id)},
            "due_change":{"id":opening["id"],"customer_id":customer["id"],"before_due_at":opening["due_at"],
                "after_due_at":rescheduled["due_at"],"before_version":1,"after_version":2,"occurred_at":deadline_time}},[opening_request["operation_id"]])
    refund_snapshot={key:refund[key] for key in ("id","payment_id","credit_id","amount_fcfa","after_unallocated_fcfa",
        "occurred_at","method","other_name","external_reference")}
    refund_snapshot.update(customer_id=customer["id"],before_unallocated_fcfa=1000)
    refund_entry=entry("customer_refund",refund_request["operation_id"],refund["occurred_at"],
        {"schema_version":1,"original_request":{**refund_request,"payment_id":payment["id"],"kind":"refund","installation_id":str(installation_id)},
            "refund":refund_snapshot},[payment_request["operation_id"]])
    allocation_snapshot={key:allocation[key] for key in ("id","payment_id","credit_id","amount_fcfa","after_unallocated_fcfa","occurred_at","allocations")}
    allocation_snapshot.update(customer_id=customer["id"],before_unallocated_fcfa=500)
    allocation_entry=entry("customer_allocation",allocation_request["operation_id"],allocation["occurred_at"],
        {"schema_version":1,"original_request":{**allocation_request,"payment_id":payment["id"],"kind":"allocation","installation_id":str(installation_id)},
            "allocation":allocation_snapshot},[payment_request["operation_id"],refund_request["operation_id"],opening_request["operation_id"]])
    return {"operations":[allocation_entry,refund_entry,collection_entry,deadline_entry,contact_entry,opening_entry,customer_entry],
        "customer":customer,"opening":opening,"payment":payment,"corrected":corrected,"rescheduled":rescheduled,"refund":refund,"allocation":allocation}


def recover_customer_flow(request,*,shop_id,foreign_shop_id,session_id,credential,survivor):
    """Use current rights and ordered explicit decisions on the real reopened server."""
    prefix=f"/api/v1/shops/{shop_id}/recovery/"
    clients=f"/api/v1/shops/{shop_id}/clients/"
    status,empty,_headers=request(clients,credential)
    assert status==200 and empty["count"]==0,(status,empty)
    journal={"operation_id":str(uuid.uuid4()),"shop_id":str(shop_id),"session_id":session_id,"format_version":1,
        "reason":"Reçus T1 survivants contrôlés après restauration T0","operations":survivor["operations"]}
    status,captured,_headers=request(prefix+"journals/",credential,journal)
    assert status==201 and captured["operation_count"]==7 and captured["business_effects_created"]==0,(status,captured)
    captures={row["operation_id"]:row["capture_id"] for row in captured["operations"]}
    entries={row["operation_type"]:row for row in survivor["operations"]}
    def path(kind):return prefix+"operations/"+captures[entries[kind]["operation_id"]]+"/"
    def preview(kind):
        status,body,_headers=request(path(kind)+"integration-preview/",credential)
        assert status==200,(status,body)
        return body
    def apply(kind,body):
        command={"operation_id":str(uuid.uuid4()),"previous_version":body["version"],"preview_token":body["preview_token"],
            "reason":"Faits et dépendances T1 contrôlés après restauration T0"}
        status,receipt,_headers=request(path(kind)+"integrate/",credential,command)
        return status,receipt,command
    old_opening=preview("customer_opening_debt");old_collection=preview("customer_collection")
    old_contact=preview("customer_update");old_deadline=preview("customer_due_change")
    old_refund=preview("customer_refund");assert not old_refund["can_apply"]
    old_allocation=preview("customer_allocation");assert not old_allocation["can_apply"]
    assert not old_opening["can_apply"] and not old_collection["can_apply"]
    assert not old_contact["can_apply"] and not old_deadline["can_apply"]
    assert request(f"/api/v1/shops/{foreign_shop_id}/recovery/operations/{captures[entries['customer_creation']['operation_id']]}/integration-preview/",credential)[0]==404
    status,created,command=apply("customer_creation",preview("customer_creation"))
    assert status==201 and created["business_receipt"]==survivor["customer"],(status,created)
    assert request(path("customer_creation")+"integrate/",credential,command)[:2]==(200,created)
    assert apply("customer_opening_debt",old_opening)[0]==409
    status,opened,command=apply("customer_opening_debt",preview("customer_opening_debt"))
    assert status==201 and opened["business_receipt"]["remaining_fcfa"]==8000,(status,opened)
    for key in ("id","customer_id","amount_fcfa","remaining_fcfa","occurred_at","due_at","due_version"):
        assert opened["business_receipt"][key]==survivor["opening"][key],key
    assert request(path("customer_opening_debt")+"integrate/",credential,command)[:2]==(200,opened)
    for kind,old in (("customer_update",old_contact),("customer_due_change",old_deadline)):
        assert apply(kind,old)[0]==409
        status,revised,command=apply(kind,preview(kind))
        assert status==201,(status,revised)
        assert request(path(kind)+"integrate/",credential,command)[:2]==(200,revised)
        if kind=="customer_update":assert revised["business_receipt"]==survivor["corrected"]
        else:
            # Replay the deadline before the payment: its acknowledgement uses
            # today's 8000 balance, never T1's later 6000 balance snapshot.
            assert revised["business_receipt"]["remaining_fcfa"]==8000
            for key in ("id","customer_id","due_at","due_version"):
                assert revised["business_receipt"][key]==survivor["rescheduled"][key],key
    status,cash,_headers=request(f"/api/v1/shops/{shop_id}/cash/",credential)
    assert status==200 and cash["count"]==0,(status,cash)
    assert apply("customer_collection",old_collection)[0]==409
    status,paid,command=apply("customer_collection",preview("customer_collection"))
    assert status==201 and paid["business_receipt"]==survivor["payment"],(status,paid)
    assert request(path("customer_collection")+"integrate/",credential,command)[:2]==(200,paid)
    stale_refund_status,stale_refund_body,_stale_refund_command=apply("customer_refund",old_refund)
    assert stale_refund_status==409,(stale_refund_status,stale_refund_body)
    status,refunded,command=apply("customer_refund",preview("customer_refund"))
    assert status==201 and refunded["business_receipt"]==survivor["refund"],(status,refunded)
    assert request(path("customer_refund")+"integrate/",credential,command)[:2]==(200,refunded)
    stale_status,stale_body,_stale_command=apply("customer_allocation",old_allocation)
    assert stale_status==409,(stale_status,stale_body)
    status,allocated,command=apply("customer_allocation",preview("customer_allocation"))
    assert status==201 and allocated["business_receipt"]==survivor["allocation"],(status,allocated)
    assert request(path("customer_allocation")+"integrate/",credential,command)[:2]==(200,allocated)
    for kind in entries:
        known=preview(kind)
        assert known["outcome"]=="already_known" and known["cash_delta_by_method_fcfa"]=={} and known["new_debt_fcfa"]==0
        status,ack,_command=apply(kind,known)
        assert status==201 and ack["outcome"]=="already_known",(status,ack)
    customer_path=clients+survivor["customer"]["id"]+"/"
    status,debts,_headers=request(customer_path+"debts/",credential)
    assert status==200 and debts["count"]==1 and debts["results"][0]["remaining_fcfa"]==5600
    assert (debts["results"][0]["due_at"],debts["results"][0]["due_version"])==(survivor["rescheduled"]["due_at"],2)
    status,corrected,_headers=request(customer_path,credential)
    assert status==200 and corrected["name"]=="Client T1 corrigé" and corrected["version"]==2
    status,payments,_headers=request(customer_path+"payments/",credential)
    assert status==200 and payments["count"]==1 and payments["results"][0]["unallocated_fcfa"]==100
    assert payments["results"][0]["installation_id"]==str(entries["customer_collection"]["origin_installation_id"])
    status,cash,_headers=request(f"/api/v1/shops/{shop_id}/cash/",credential)
    assert status==200 and cash["count"]==2 and sorted(row["amount_fcfa"] for row in cash["results"])==[-500,3000]
    status,regularizations,_headers=request(customer_path+"payments/"+survivor["payment"]["id"]+"/regularizations/",credential)
    assert status==200 and regularizations["count"]==2
    history={row["id"]:row for row in regularizations["results"]}
    assert history[survivor["refund"]["id"]]==survivor["refund"]
    assert history[survivor["allocation"]["id"]]==survivor["allocation"]
    return {"t0_archive_t1_receipts_recovered":True,"recovered_customer_debt_collection_uuids":True,
        "recovered_remaining_debt_fcfa":5600,"recovered_unallocated_fcfa":100,"recovered_cash_fcfa":2500,
        "dependency_stale_previews":409,"dependency_exact_replays":True,"dependency_already_known_no_effect":True,
        "recovered_contact_and_deadline_revisions":True,"deadline_replayed_before_payment_without_balance_reset":True,
        "recovered_real_customer_refund_fcfa":500,"refund_replay_without_second_cash":True,
        "recovered_explicit_allocation_fcfa":400,"allocation_without_cash_or_second_debt_reduction":True}
