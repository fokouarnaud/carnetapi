"""Real HTTP recovered-sale flow on a disposable database; supplies fresh auth.

Does not qualify OTP, Android, production maintenance reopening or SMTP.
"""
from datetime import datetime,timedelta,timezone as utc
from contextlib import ExitStack
from copy import deepcopy
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
    with TemporaryDirectory(prefix="recovery-sale-http-") as directory,ExitStack() as resources:
        os.environ["DJANGO_SETTINGS_MODULE"]="config.test_settings"
        os.environ["APP_DB_PATH"]=str(Path(directory)/"recovery.sqlite3")
        import django
        django.setup()
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.db import connections
        resources.callback(connections.close_all)
        from django.utils import timezone
        from rest_framework_simplejwt.tokens import AccessToken
        from backups.models import RestoreAccessGeneration,RestoreAccessReview,RestoreAccessDecision,RestoreSession,RecoveryIntegrationDecision,RecoverySaleHoldDecision
        from backups.access_models import PERMISSION_FIELDS
        from cash.models import CashMovement
        from catalogue.models import Product
        from common.models import Activity
        from identity.models import User,Shop,Installation,DeviceSession,Membership
        from sales.models import Sale,SaleLine
        from stock.models import StockMovement,StockAlertEvent,StockReservation
        from stock.services.stock_queries import with_stock

        call_command("migrate",verbosity=0)
        owner=User.objects.create_user(username="current-owner")
        technical=User.objects.create_superuser(username="current-technical",password=None)
        source=User.objects.create_user(username="withdrawn-source",is_active=False)
        shops=[Shop.objects.create(owner=owner,name=name) for name in ("A","B")]
        membership=Membership.objects.create(user=source,shop=shops[0],is_active=False)
        old_phone=Installation.objects.create(user=source,revoked_at=timezone.now())
        current_phone=Installation.objects.create(user=owner)
        session=DeviceSession.objects.create(installation=current_phone,expires_at=timezone.now()+timedelta(days=1))
        token=AccessToken.for_user(owner)
        token["session_id"]=str(session.id)
        generation=RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),initiated_by=technical.id)
        review=RestoreAccessReview.objects.create(generation=generation,shop=shops[0],user=owner,
            archived_role="owner",was_active=True,archived_permissions=dict.fromkeys(PERMISSION_FIELDS,True))
        RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=technical,
            previous_version=0,version=1,state="confirmed",reason="Contrôle de la fixture privée")
        registry=RestoreSession.objects.create(generation=generation,inventory_recorded_at=timezone.now())
        product=Product.objects.create(shop=shops[0],name="Tarif actuel",unit_kind="piece",unit_label="pièce",
            sale_price_fcfa=900,version=8,stock_threshold_milli=2000)
        opening=Activity.objects.create(shop=shops[0],actor=owner,action="opening_stock",target_id=product.id,reason="Ouverture")
        StockMovement.objects.create(product=product,actor=owner,operation=opening,kind="opening",quantity_milli=3000,
            unit_cost_micros=300000000,occurred_at=datetime(2026,10,20,9,tzinfo=utc.utc),reason="Ouverture")
        operation,sale,payment=[str(uuid.uuid4()) for _ in range(3)]
        lines=[{"id":str(uuid.uuid4()),"product_id":str(product.id),"product_version":7,"pack_id":None,
            "product_name":"🛒"*150,"unit_kind":"piece","unit_label":"📦"*80,"quantity_milli":1000,
            "factor_milli":1000,"base_quantity_milli":1000,"unit_price_fcfa":500,"reference_fcfa":500,
            "own_discount_fcfa":0,"global_discount_fcfa":0,"net_fcfa":500} for _ in range(500)]
        request={"id":sale,"operation_id":operation,"mode":"cash",
            "lines":[{key:row[key] for key in ("id","product_id","product_version","pack_id","quantity_milli","own_discount_fcfa")} for row in lines],
            "payments":[{"id":payment,"method":"cash","amount_fcfa":250000,"received_fcfa":250000,"returned_fcfa":0}]}
        frozen={"id":sale,"mode":"cash","customer_id":None,"due_at":None,"reference_total_fcfa":250000,
            "discount_total_fcfa":0,"net_total_fcfa":250000,"lines":lines,
            "payments":[{**request["payments"][0],"other_name":"","external_reference":""}]}
        journal={"operation_id":str(uuid.uuid4()),"shop_id":str(shops[0].id),"session_id":str(registry.id),"format_version":1,
            "reason":"Copie remise par un accès retiré, contrôlée par le responsable","operations":[{
                "operation_id":operation,"origin_user_id":str(source.id),"origin_installation_id":str(old_phone.id),
                "operation_type":"sale","occurred_at":"2026-10-22T09:00:00Z","origin_recorded_at":"2026-10-22T09:01:00Z",
                "dependencies":[],"payload":{"schema_version":1,"original_request":request,"sale":frozen}}]}
        server=make_server("127.0.0.1",0,get_wsgi_application(),handler_class=QuietHandler)
        thread=Thread(target=server.serve_forever,daemon=True)
        thread.start()
        root=f"http://127.0.0.1:{server.server_port}/api/v1/shops/"
        try:
            def http(path,data=None):
                body=json.dumps(data,ensure_ascii=False).encode() if data is not None else None
                req=Request(root+path,data=body,headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"})
                try:
                    with urlopen(req,timeout=60) as response:return response.status,json.load(response)
                except HTTPError as error:
                    with error:return error.code,json.load(error)
            prefix=f"{shops[0].id}/recovery/"
            status,captured=http(prefix+"journals/",journal)
            assert status==201 and captured["business_effects_created"]==0,(status,captured)
            capture=captured["operations"][0]["capture_id"]
            path=prefix+f"operations/{capture}/"
            status,preview=http(path+"integration-preview/")
            assert status==200 and preview["can_apply"] and preview["snapshot"]["net_total_fcfa"]==250000
            assert http(f"{shops[1].id}/recovery/operations/{capture}/integration-preview/")[0]==404
            hold_command={"operation_id":str(uuid.uuid4()),"previous_version":preview["review_version"],
                "preview_token":preview["preview_token"],"state":"pending","reason":"Copie à examiner avant intégration"}
            status,held=http(path+"review/",hold_command)
            assert status==201 and held["state"]=="pending",(status,held)
            assert http(path+"review/",hold_command)==(200,held)
            assert (Sale.objects.count(),CashMovement.objects.count(),StockReservation.objects.count())==(0,0,1)
            assert StockReservation.objects.get().quantity_milli==500000
            status,queue=http(prefix+"sales-review/?state=pending&page_size=1")
            assert status==200 and queue["count"]==1 and queue["totals"]["pending"]["payments_fcfa"]=="250000"
            status,preview=http(path+"integration-preview/")
            assert status==200 and preview["review_state"]=="pending"
            assert preview["stocks"][0]["available_after_milli"]=="-497000"
            assert preview["stocks"][0]["reserved_after_milli"]=="0"
            command={"operation_id":str(uuid.uuid4()),"previous_version":0,"preview_token":preview["preview_token"],
                "reason":"500 lignes physiques et montants historiques vérifiés"}
            status,receipt=http(path+"integrate/",command)
            assert status==201 and receipt["outcome"]=="applied",(status,receipt)
            assert receipt["business_receipt"]["actor_id"]==str(source.id)
            assert http(path+"integrate/",command)==(200,receipt)
            status,known=http(path+"integration-preview/")
            assert status==200 and known["outcome"]=="already_known"
            status,again=http(path+"integrate/",{**command,"operation_id":str(uuid.uuid4()),
                "previous_version":known["version"],"preview_token":known["preview_token"]})
            assert status==201 and again["outcome"]=="already_known"
            assert (Sale.objects.count(),SaleLine.objects.count(),CashMovement.objects.count(),StockAlertEvent.objects.count())==(1,500,1,1)
            assert CashMovement.objects.get().amount_fcfa==250000
            assert with_stock(Product.objects.filter(id=product.id)).get().stock_available_milli=="-497000"
            assert RecoveryIntegrationDecision.objects.count()==2
            assert RecoverySaleHoldDecision.objects.count()==2
            assert StockReservation.objects.get().state=="validated"
            declined=deepcopy(journal)
            declined["operation_id"]=str(uuid.uuid4())
            entry=declined["operations"][0]
            entry["operation_id"]=str(uuid.uuid4())
            payload=entry["payload"]
            payload["sale"].update(id=str(uuid.uuid4()),reference_total_fcfa=500,net_total_fcfa=500)
            payload["sale"]["lines"]=[{**payload["sale"]["lines"][0],"id":str(uuid.uuid4())}]
            payload["sale"]["payments"][0].update(id=str(uuid.uuid4()),amount_fcfa=500,received_fcfa=500)
            payload["original_request"].update(id=payload["sale"]["id"],operation_id=entry["operation_id"],
                lines=[{**payload["original_request"]["lines"][0],"id":payload["sale"]["lines"][0]["id"]}],
                payments=[{**payload["original_request"]["payments"][0],"id":payload["sale"]["payments"][0]["id"],"amount_fcfa":500,"received_fcfa":500}])
            status,new_copy=http(prefix+"journals/",declined)
            assert status==201
            refused_path=prefix+f"operations/{new_copy['operations'][0]['capture_id']}/"
            status,before=http(refused_path+"integration-preview/")
            status,_held=http(refused_path+"review/",{**hold_command,"operation_id":str(uuid.uuid4()),"preview_token":before["preview_token"]})
            assert status==201
            status,before=http(refused_path+"integration-preview/")
            status,refused=http(refused_path+"review/",{**hold_command,"operation_id":str(uuid.uuid4()),"state":"refused",
                "previous_version":before["review_version"],"preview_token":before["preview_token"],"reason":"Départ à rapprocher, sans remboursement automatique"})
            assert status==201 and refused["state"]=="refused"
            status,blocked=http(refused_path+"integration-preview/")
            assert status==200 and not blocked["can_apply"]
            assert http(refused_path+"integrate/",{**command,"operation_id":str(uuid.uuid4()),"previous_version":blocked["version"],
                "preview_token":blocked["preview_token"]})[0]==409
            status,queue=http(prefix+"sales-review/?state=refused")
            assert status==200 and queue["count"]==1 and queue["totals"]["refused"]["payments_fcfa"]=="500"
            assert (Sale.objects.count(),CashMovement.objects.count(),CashMovement.objects.get().amount_fcfa)==(1,1,250000)
            assert with_stock(Product.objects.filter(id=product.id)).get().stock_available_milli=="-498000"
            loss_id=str(uuid.uuid4())
            status,loss=http(f"{shops[0].id}/products/{product.id}/losses/",{"id":loss_id,"operation_id":str(uuid.uuid4()),
                "quantity_milli":1000,"occurred_at":"2026-10-22T10:00:00Z","reason":"Même départ physique déjà constaté"})
            assert status==201,(status,loss)
            status,movements=http(f"{shops[0].id}/products/{product.id}/stock-shortage-reviews/movements/?page_size=100")
            assert status==200,(status,movements)
            movement_id=next((row["id"] for row in movements["results"] if row["source_id"]==loss_id),None)
            while movement_id is None and movements["next"]:
                next_page=movements["next"]
                assert next_page.startswith(root),next_page
                status,movements=http(next_page[len(root):])
                assert status==200,(status,movements)
                movement_id=next((row["id"] for row in movements["results"] if row["source_id"]==loss_id),None)
            assert movement_id is not None,"Physical loss missing from the complete paginated movement list"
            status,current=http(refused_path+"integration-preview/")
            assert status==200
            reservation=current["stocks"][0]["reservation_id"]
            match_path=refused_path+f"reservations/{reservation}/"
            status,match=http(match_path+f"reconciliation-preview/?movement_id={movement_id}&quantity_milli=1000")
            assert status==200 and match["can_apply"],(status,match)
            assert (match["available_before_milli"],match["available_after_milli"])==("-499000","-498000")
            movement_count=StockMovement.objects.count()
            match_command={"operation_id":str(uuid.uuid4()),"movement_id":movement_id,"quantity_milli":1000,
                "preview_token":match["preview_token"],"reason":"Rapprochement explicite des deux constats"}
            status,matched=http(match_path+"reconciliations/",match_command)
            assert status==201,(status,matched)
            assert http(match_path+"reconciliations/",match_command)==(200,matched)
            assert (StockMovement.objects.count(),CashMovement.objects.count(),Sale.objects.count())==(movement_count,1,1)
            assert with_stock(Product.objects.filter(id=product.id)).get().stock_reserved_milli=="0"
            assert not http(refused_path+"integration-preview/")[1]["can_apply"]
            assert http(match_path+"reconciliations/")[1]["count"]==1
            source.refresh_from_db();membership.refresh_from_db();old_phone.refresh_from_db()
            assert not source.is_active and not membership.is_active and old_phone.revoked_at is not None
            RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=technical,
                previous_version=1,version=2,state="refused",reason="Contrôle retiré dans la fixture")
            assert http(path+"integrate/",command)[0]==404
            print(json.dumps({"http":"passed","lines":500,"cash_fcfa":250000,"stock_milli":-497000,
                "sales":1,"cash_movements":1,"alerts":1,"review_decisions":2,"source_reactivated":False,
                "held_then_validated":True,"refused_money_fcfa":500,"refused_reserved_milli":0,"available_milli":-498000,
                "reconciled_quantity_milli":1000,"reconciliation_extra_movements":0,"reconciliation_extra_cash":0,
                "foreign_scope":404,"revoked_reviewer":404,"journal_utf8_bytes":len(json.dumps(journal,ensure_ascii=False).encode())}))
        finally:
            server.shutdown();thread.join(timeout=5);server.server_close();connections.close_all()


if __name__=="__main__":main()
