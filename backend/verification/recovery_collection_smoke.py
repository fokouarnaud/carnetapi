"""Exercise recovered collections over real loopback HTTP on private SQLite.

Fresh fixture authentication does not qualify OTP, Android or hosted recovery.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime,timedelta,timezone as utc
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request,urlopen
import uuid
from wsgiref.simple_server import make_server,WSGIRequestHandler


class QuietHandler(WSGIRequestHandler):
    def log_message(self,format,*args):pass


def main():
    with TemporaryDirectory(prefix="recovery-collection-http-") as directory,ExitStack() as resources:
        os.environ["DJANGO_SETTINGS_MODULE"]="config.test_settings"
        os.environ["APP_DB_PATH"]=str(Path(directory)/"recovery.sqlite3")
        import django
        django.setup()
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.db import connections
        from django.utils import timezone
        from rest_framework_simplejwt.tokens import AccessToken
        from backups.models import RestoreAccessGeneration,RestoreAccessReview,RestoreAccessDecision,RestoreSession,RecoveryJournalOperation,RecoveryCollectionReview,RecoveryCollectionReviewDecision
        from backups.access_models import PERMISSION_FIELDS
        from cash.models import CashMovement
        from common.models import Activity
        from customers.models import Customer,CustomerDebt,CustomerPayment,CustomerLedgerEntry
        from identity.models import User,Shop,Installation,DeviceSession,Membership
        from sales.models import Sale
        from stock.models import StockMovement
        resources.callback(connections.close_all)
        call_command("migrate",verbosity=0)
        owner=User.objects.create_user(username="collection-current-owner")
        technical=User.objects.create_superuser(username="collection-technical",password=None)
        source=User.objects.create_user(username="collection-withdrawn-source")
        shops=[Shop.objects.create(owner=owner,name=name) for name in ("A","B")]
        membership=Membership.objects.create(user=source,shop=shops[0],is_active=False)
        old_phone=Installation.objects.create(user=source,revoked_at=timezone.now())
        old_session=DeviceSession.objects.create(installation=old_phone,expires_at=timezone.now()+timedelta(days=1))
        old_token=AccessToken.for_user(source);old_token["session_id"]=str(old_session.id)
        source.is_active=False;source.save(update_fields=["is_active"])
        phone=Installation.objects.create(user=owner)
        session=DeviceSession.objects.create(installation=phone,expires_at=timezone.now()+timedelta(days=1))
        token=AccessToken.for_user(owner);token["session_id"]=str(session.id)
        generation=RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),initiated_by=technical.id)
        review=RestoreAccessReview.objects.create(generation=generation,shop=shops[0],user=owner,
            archived_role="owner",was_active=True,archived_permissions=dict.fromkeys(PERMISSION_FIELDS,True))
        RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=technical,
            previous_version=0,version=1,state="confirmed",reason="Droits de la fixture privée vérifiés")
        registry=RestoreSession.objects.create(generation=generation,inventory_recorded_at=timezone.now())
        customer=Customer.objects.create(shop=shops[0],name="Client connu")
        debts=[]
        for amount in (3000,7000):
            audit=Activity.objects.create(shop=shops[0],actor=owner,action="customer_debt_opened",target_id=uuid.uuid4(),reason="Solde connu")
            debts.append(CustomerDebt.objects.create(id=audit.target_id,shop=shops[0],customer=customer,actor=owner,
                installation=phone,operation=audit,amount_fcfa=amount,remaining_fcfa=amount,
                occurred_at=datetime(2026,10,20,9,tzinfo=utc.utc)))
        payment,operation=uuid.uuid4(),uuid.uuid4()
        parts=[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":2000,"received_fcfa":5000,"returned_fcfa":3000,
                "other_name":"","external_reference":""},
            {"id":str(uuid.uuid4()),"method":"orange","amount_fcfa":3000,"other_name":"","external_reference":"receipt-phone"}]
        allocations=[{"id":str(uuid.uuid4()),"debt_id":str(debt.id),"amount_fcfa":amount}
            for debt,amount in zip(debts,(3000,2000),strict=True)]
        request={"id":str(payment),"operation_id":str(operation),"parts":parts,"allocations":allocations}
        snapshot={"id":str(payment),"customer_id":str(customer.id),"amount_fcfa":5000,"unallocated_fcfa":0,"requires_regularization":False,
            "parts":[{"received_fcfa":None,"returned_fcfa":None,**part} for part in parts],
            "allocations":[{"id":row["id"],"debt_id":row["debt_id"],"requested_amount_fcfa":row["amount_fcfa"],
                "applied_amount_fcfa":row["amount_fcfa"]} for row in allocations]}
        journal={"operation_id":str(uuid.uuid4()),"shop_id":str(shops[0].id),"session_id":str(registry.id),"format_version":1,
            "reason":"Copie survivante remise par l'ancien vendeur","operations":[{
                "operation_id":str(operation),"origin_user_id":str(source.id),"origin_installation_id":str(old_phone.id),
                "operation_type":"customer_collection","occurred_at":"2026-10-22T09:00:00Z","origin_recorded_at":"2026-10-22T09:01:00Z",
                "dependencies":[],"payload":{"schema_version":1,"original_request":request,"collection":snapshot}}]}
        server=make_server("127.0.0.1",0,get_wsgi_application(),handler_class=QuietHandler)
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        root=f"http://127.0.0.1:{server.server_port}/api/v1/shops/"
        try:
            def http(path,data=None,authorization=None):
                body=json.dumps(data).encode() if data is not None else None
                req=Request(root+path,data=body,headers={"Authorization":f"Bearer {authorization or token}","Content-Type":"application/json"})
                try:
                    with urlopen(req,timeout=60) as response:return response.status,json.load(response)
                except HTTPError as error:
                    with error:return error.code,json.load(error)
            prefix=f"{shops[0].id}/recovery/"
            clients=f"{shops[0].id}/clients/{customer.id}/payments/"
            assert http(prefix+"journals/",journal,authorization=old_token)[0]==401
            status,captured=http(prefix+"journals/",journal)
            assert status==201 and captured["business_effects_created"]==0,(status,captured)
            capture=captured["operations"][0]["capture_id"]
            path=prefix+f"operations/{capture}/"
            status,preview=http(path+"integration-preview/")
            assert status==200 and preview["can_apply"] and preview["new_customer_credit_fcfa"]==0,(status,preview)
            review_command={"operation_id":str(uuid.uuid4()),"previous_version":preview["review_version"],
                "preview_token":preview["preview_token"],"state":"pending","reason":"Montants en attente d'examen"}
            status,pending=http(path+"collection-review/",review_command)
            assert status==201 and pending["state"]=="pending",(status,pending)
            assert http(path+"collection-review/",review_command)==(200,pending)
            status,queue=http(prefix+"collections-review/?state=pending&page_size=1")
            assert status==200 and queue["totals"]["pending"]["amount_fcfa"]=="5000" and queue["count"]==1
            assert (CustomerPayment.objects.count(),CashMovement.objects.count(),StockMovement.objects.count())==(0,0,0)
            status,preview=http(path+"integration-preview/")
            assert status==200 and preview["review_state"]=="pending" and preview["review_version"]==1
            assert http(clients,request)[0]==409
            assert http(f"{shops[1].id}/recovery/operations/{capture}/integration-preview/")[0]==404
            native={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),
                "parts":[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":4000}],
                "allocations":[{"id":str(uuid.uuid4()),"debt_id":str(debt.id),"amount_fcfa":amount}
                    for debt,amount in zip(debts,(3000,1000),strict=True)]}
            status,native_receipt=http(clients,native);assert status==201,(status,native_receipt)
            command={"operation_id":str(uuid.uuid4()),"previous_version":preview["version"],"preview_token":preview["preview_token"],
                "reason":"Faits encaissés vérifiés par le responsable actuel"}
            assert http(path+"integrate/",command)[0]==409
            assert CustomerPayment.objects.count()==1
            status,preview=http(path+"integration-preview/")
            assert status==200 and preview["new_customer_credit_fcfa"]==3000
            assert [row["applied_fcfa"] for row in preview["allocation_impacts"]]==[0,2000]
            command.update(preview_token=preview["preview_token"])
            status,receipt=http(path+"integrate/",command)
            assert status==201 and receipt["business_receipt"]["unallocated_fcfa"]==3000,(status,receipt)
            assert receipt["business_receipt"]["actor_id"]==str(source.id)
            assert receipt["business_receipt"]["parts"][0]["received_fcfa"]==5000
            assert http(path+"integrate/",command)==(200,receipt)
            assert RecoveryCollectionReview.objects.get().state=="validated" and RecoveryCollectionReviewDecision.objects.count()==2
            status,history=http(path+"collection-review-decisions/?page_size=1")
            assert status==200 and history["count"]==2 and history["results"]==[pending]
            assert sorted(CashMovement.objects.values_list("amount_fcfa",flat=True))==[2000,3000,4000]
            assert (Sale.objects.count(),StockMovement.objects.count())==(0,0)
            assert list(CustomerDebt.objects.order_by("amount_fcfa").values_list("remaining_fcfa",flat=True))==[0,4000]
            refund={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"method":"cash","amount_fcfa":1000,
                "reason":"Remboursement réellement remis au client"}
            status,refunded=http(clients+f"{payment}/refund/",refund);assert status==201,(status,refunded)
            status,current=http(clients)
            recovered=next(row for row in current["results"] if row["id"]==str(payment))
            assert status==200 and recovered["unallocated_fcfa"]==2000 and recovered["actor_id"]==str(source.id)
            status,known=http(path+"integration-preview/")
            assert status==200 and known["outcome"]=="already_known" and known["cash_delta_by_method_fcfa"]=={}
            status,ack=http(path+"integrate/",{**command,"operation_id":str(uuid.uuid4()),
                "previous_version":known["version"],"preview_token":known["preview_token"]})
            assert status==201 and ack["business_receipt"]==receipt["business_receipt"],(status,ack)
            assert CustomerPayment.objects.count()==2 and CashMovement.objects.count()==4 and CustomerLedgerEntry.objects.count()==3
            assert sum(CashMovement.objects.values_list("amount_fcfa",flat=True))==8000
            status,statement=http(f"{shops[0].id}/clients/{customer.id}/statement/")
            assert status==200,(status,statement)
            historical=next(row for row in statement["results"] if row["operation_id"]==str(operation))
            assert historical["actor_id"]==str(source.id) and historical["delta_fcfa"]==-5000
            assert [row["applied_amount_fcfa"] for row in historical["allocations"]]==[0,2000]
            status,personal=http(f"{shops[0].id}/cash/?scope=personal")
            assert status==200 and personal["count"]==2,(status,personal)
            assert sum(row["amount_fcfa"] for row in personal["results"])==3000
            assert RecoveryJournalOperation.objects.get(id=capture).payload["collection"]==snapshot
            declined=deepcopy(journal);declined["operation_id"]=str(uuid.uuid4())
            entry=declined["operations"][0];entry["operation_id"]=str(uuid.uuid4())
            raw=entry["payload"]["original_request"];frozen=entry["payload"]["collection"]
            raw.update(id=str(uuid.uuid4()),operation_id=entry["operation_id"]);frozen["id"]=raw["id"]
            for field in ("parts","allocations"):
                for asked,original in zip(raw[field],frozen[field],strict=True):
                    asked["id"]=str(uuid.uuid4());original["id"]=asked["id"]
            status,copied=http(prefix+"journals/",declined);assert status==201,(status,copied)
            refused_path=prefix+f"operations/{copied['operations'][0]['capture_id']}/"
            status,before=http(refused_path+"integration-preview/");assert status==200 and before["can_mark_pending"]
            status,_staged=http(refused_path+"collection-review/",{**review_command,"operation_id":str(uuid.uuid4()),"preview_token":before["preview_token"]})
            assert status==201
            status,before=http(refused_path+"integration-preview/")
            status,refused=http(refused_path+"collection-review/",{**review_command,"operation_id":str(uuid.uuid4()),"state":"refused",
                "previous_version":before["review_version"],"preview_token":before["preview_token"],"reason":"Argent à régulariser explicitement"})
            assert status==201 and refused["state"]=="refused",(status,refused)
            status,blocked=http(refused_path+"integration-preview/")
            assert status==200 and not blocked["can_apply"]
            assert http(refused_path+"integrate/",{**command,"operation_id":str(uuid.uuid4()),"preview_token":blocked["preview_token"],"previous_version":blocked["version"]})[0]==409
            status,queue=http(prefix+"collections-review/?state=refused")
            assert status==200 and queue["totals"]["refused"]["amount_fcfa"]=="5000" and queue["count"]==1
            assert (CustomerPayment.objects.count(),CashMovement.objects.count(),CustomerLedgerEntry.objects.count())==(2,4,3)
            assert sum(CashMovement.objects.values_list("amount_fcfa",flat=True))==8000
            source.refresh_from_db();membership.refresh_from_db();old_phone.refresh_from_db()
            assert not source.is_active and not membership.is_active and old_phone.revoked_at is not None
            RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=technical,
                previous_version=1,version=2,state="refused",reason="Contrôle actuel retiré")
            assert http(path+"integrate/",command)[0]==404
            print(json.dumps({"http":"passed","recovered_money_fcfa":5000,"recovered_allocation_fcfa":2000,
                "initial_unallocated_fcfa":3000,"after_real_refund_fcfa":2000,"total_cash_fcfa":8000,
                "payments":2,"cash_movements":4,"personal_cash_fcfa":3000,"statement_origin_preserved":True,
                "pending_then_validated":True,"refused_money_fcfa":5000,"refusal_extra_cash":0,
                "stock_movements":0,"source_reactivated":False,
                "stale_preview":409,"native_bypass":409,"old_session":401,"foreign_shop":404,"revoked_reviewer":404}))
        finally:
            server.shutdown();thread.join(timeout=5);server.server_close();connections.close_all()


if __name__=="__main__":main()
