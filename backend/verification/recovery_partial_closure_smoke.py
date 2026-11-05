"""Exercise partial closure over real WSGI on a private SQLite recovery fixture.

The source sale is recorded through HTTP, retained outside SQLite, then removed
by restoring this fixture's baseline with SQLite backup. This is not the
production archive/activation procedure and does not exercise OTP.
"""
from contextlib import closing, contextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import sqlite3
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid
from wsgiref.simple_server import make_server, WSGIRequestHandler


class QuietHandler(WSGIRequestHandler):
    def log_message(self,format,*args):
        pass


def main():
    root=Path(__file__).resolve().parents[1]/"runtime-control"/"verification"/("partial-closure-"+uuid.uuid4().hex)
    root.mkdir(parents=True)
    database,baseline,gate=root/"business.sqlite3",root/"baseline.sqlite3",root/"gate.sqlite3"
    os.environ["DJANGO_SETTINGS_MODULE"]="config.test_settings"
    os.environ["APP_DB_PATH"]=str(database)
    import django
    django.setup()
    from django.core.management import call_command
    from django.core.wsgi import get_wsgi_application
    from django.db import connections
    from django.test import override_settings
    from django.utils import timezone
    from rest_framework_simplejwt.tokens import AccessToken
    from backups.models import (RestoreAccessGeneration,RestoreAccessReview,RestoreSession,RecoveryDevice,
        RecoveryJournalOperation,RecoverySaleHold,RecoveryIntegrationDecision,RecoveryClosure)
    from backups.access_models import PERMISSION_FIELDS
    from backups.services.access_decisions import decide_responsible_access
    from backups.services.restore_gate import initialize_gate
    from backups.services.recovery_sale_snapshot import immutable_receipt_snapshot
    from cash.models import CashMovement
    from catalogue.models import Product
    from common.models import Activity
    from identity.models import User,Shop,Installation,DeviceSession
    from sales.models import Sale
    from stock.models import StockMovement,StockReservation

    call_command("migrate",verbosity=0)
    technical=User.objects.create_superuser(username="closure-http-technical",password=None)
    owner=User.objects.create_user(username="closure-http-owner")
    shops=[Shop.objects.create(owner=owner,name=name) for name in ("Closure A","Closure B")]
    generation=RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),initiated_by=technical.id)
    registry=RestoreSession.objects.create(generation=generation,snapshot_at=timezone.now()-timedelta(days=1),
        inventory_recorded_at=timezone.now())
    flags=dict.fromkeys(PERMISSION_FIELDS,True)
    for shop in shops:
        review=RestoreAccessReview.objects.create(generation=generation,shop=shop,user=owner,
            archived_role="owner",was_active=True,archived_permissions=flags)
        decide_responsible_access(actor=technical,review_id=review.id,operation_id=uuid.uuid4(),previous_version=0,
            state="confirmed",reason="Identité et responsabilité actuelles contrôlées dans cette fixture")

    def token(user,label):
        installation=Installation.objects.create(user=user,label=label)
        session=DeviceSession.objects.create(installation=installation,expires_at=timezone.now()+timedelta(days=1))
        value=AccessToken.for_user(user);value["session_id"]=str(session.id)
        return installation,session,str(value)
    main_device,main_session,manager_token=token(owner,"Responsable actuellement autorisé")
    lost_device,lost_session,lost_token=token(owner,"Appareil A devenu définitivement indisponible")
    other_device,_,_other_token=token(owner,"Appareil B connu")
    _technical_device,technical_session,technical_token=token(technical,"Console technique actuelle")
    missing=RecoveryDevice.objects.create(session=registry,shop=shops[0],origin_installation_id=lost_device.id,
        origin_user_id=owner.id,label=lost_device.label)
    RecoveryDevice.objects.create(session=registry,shop=shops[1],origin_installation_id=other_device.id,
        origin_user_id=owner.id,label=other_device.label)
    products=[]
    for shop,quantity in zip(shops,(5000,7000),strict=True):
        product=Product.objects.create(shop=shop,name="Produit de "+shop.name,unit_kind="piece",unit_label="pièce",sale_price_fcfa=500)
        event=Activity.objects.create(shop=shop,actor=owner,action="stock_opened",target_id=product.id,reason="Stock de départ de la fixture")
        StockMovement.objects.create(product=product,actor=owner,operation=event,kind="opening",quantity_milli=quantity,
            unit_cost_micros=100000000,occurred_at=timezone.now()-timedelta(days=2),reason=event.reason)
        products.append(product)
    initialize_gate(gate,database,operation_id=uuid.uuid4(),requested_by=technical.id,
        reason="Fixture WSGI privée avec barrière ouverte, sans activation de restauration")

    @contextmanager
    def serving():
        with override_settings(RESTORE_GATE_PATH=gate):
            server=make_server("127.0.0.1",0,get_wsgi_application(),handler_class=QuietHandler)
            thread=Thread(target=server.serve_forever,daemon=True);thread.start()
            origin=f"http://127.0.0.1:{server.server_port}/api/v1/"
            def request(path,bearer=manager_token,data=None):
                headers={"Authorization":"Bearer "+bearer,"Content-Type":"application/json","Accept-Language":"en"}
                req=Request(origin+path,data=None if data is None else json.dumps(data).encode("utf-8"),headers=headers)
                try:response=urlopen(req,timeout=30)
                except HTTPError as error:response=error
                with response:return response.status,json.load(response)
            try:yield request
            finally:
                server.shutdown();thread.join(timeout=5);server.server_close();connections.close_all()

    def journal(entry):
        return {"operation_id":str(uuid.uuid4()),"session_id":str(registry.id),"shop_id":str(shops[0].id),
            "format_version":1,"reason":"Copie survivante remise au responsable actuel","operations":[entry]}
    prefix=f"shops/{shops[0].id}/"
    recovery=prefix+"recovery/"
    closure_path=f"recovery/sessions/{registry.id}/"
    with serving() as request:
        raw={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"name":"Client natif conservé dans le socle"}
        status,customer=request(prefix+"clients/",data=raw);assert status==201,(status,customer)
        known_entry={"operation_id":raw["operation_id"],"origin_user_id":str(owner.id),
            "origin_installation_id":str(main_device.id),"operation_type":"customer_creation", "occurred_at":customer["created_at"],
            "origin_recorded_at":timezone.now().isoformat(),"dependencies":[],"payload":{"schema_version":1,
                "original_request":Activity.objects.get(id=raw["operation_id"]).details["request"],
                "customer":{key:customer[key] for key in ("id","name","phone","version","created_at","updated_at")}}}
        status,known_deposit=request(recovery+"journals/",data=journal(known_entry));assert status==201,(status,known_deposit)
        known_path=recovery+"operations/"+known_deposit["operations"][0]["capture_id"]+"/"
        status,preview=request(known_path+"integration-preview/");assert status==200 and preview["outcome"]=="already_known",(status,preview)
        known_command={"operation_id":str(uuid.uuid4()),"previous_version":preview["version"],
            "preview_token":preview["preview_token"],"reason":"Ancienne fiche et reçu natif contrôlés"}
        status,known_receipt=request(known_path+"integrate/",data=known_command)
        assert status==201 and known_receipt["outcome"]=="already_known",(status,known_receipt)
        connections.close_all()
        with closing(sqlite3.connect(database)) as source,closing(sqlite3.connect(baseline)) as target:source.backup(target)

        raw_sale={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"mode":"cash",
            "lines":[{"id":str(uuid.uuid4()),"product_id":str(products[0].id),"product_version":1,"quantity_milli":1000}],
            "payments":[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":500,"received_fcfa":500,"returned_fcfa":0}]}
        status,sale=request(prefix+"sales/",lost_token,data=raw_sale)
        assert status==201 and sale["net_total_fcfa"]==500,(status,sale)
        late_entry={"operation_id":raw_sale["operation_id"],"origin_user_id":str(owner.id),
            "origin_installation_id":str(lost_device.id),"operation_type":"sale","occurred_at":sale["occurred_at"],
            "origin_recorded_at":timezone.now().isoformat(),"dependencies":[],"payload":{"schema_version":1,
                "original_request":Activity.objects.get(id=raw_sale["operation_id"]).details["request"],
                "sale":immutable_receipt_snapshot(sale)}}
        assert request(prefix+"cash/")[1]["totals_by_method_fcfa"]=={"cash":500}
        assert int(request(prefix+"products/")[1]["results"][0]["stock_validated_milli"])==4000
    # Replace only this private verification fixture, after both WSGI and ORM
    # connections are closed. The real production restore path is not exercised.
    with closing(sqlite3.connect(baseline)) as source,closing(sqlite3.connect(database)) as target:source.backup(target)
    Installation.objects.filter(id=lost_device.id).update(revoked_at=timezone.now())
    DeviceSession.objects.filter(id=lost_session.id).update(revoked_at=timezone.now())
    connections.close_all()

    with serving() as request:
        assert request(prefix+"sales/",lost_token)[0]==401
        assert request(prefix+"sales/")[1]["count"]==0
        assert request(prefix+"cash/")[1]["totals_by_method_fcfa"]=={}
        assert int(request(prefix+"products/")[1]["results"][0]["stock_validated_milli"])==5000
        assert request(closure_path+"closure-preview/")[0]==403
        status,preview=request(closure_path+"closure-preview/",technical_token)
        assert status==200 and preview["can_close_partial"] and not preview["can_close_complete"],(status,preview)
        closure_command={"operation_id":str(uuid.uuid4()),"restore_session_id":str(registry.id),"kind":"partial",
            "preview_token":preview["preview_token"],"reason":"Appareil A définitivement indisponible, données manquantes explicitement conservées",
            "missing_device_ids":[str(missing.id)]}
        assert request(closure_path+"closures/",technical_token,{**closure_command,"kind":"complete","missing_device_ids":[]})[0]==400
        status,closed=request(closure_path+"closures/",technical_token,closure_command)
        assert status==201 and closed["state"]=="closed_partial" and not closed["exhaustive_recovery_guaranteed"],(status,closed)
        assert request(closure_path+"closures/",technical_token,closure_command)==(200,closed)
        for shop,missing_count,stock in ((shops[0],1,5000),(shops[1],0,7000)):
            status,body=request(f"shops/{shop.id}/products/");assert status==200,(status,body)
            state=body["restoration"]
            assert state["state"]=="closed_partial" and state["missing_devices"]==missing_count
            assert state["missing_data"]==(missing_count>0) and state["late_operations"]==0
            assert int(body["results"][0]["stock_validated_milli"])==stock
            assert str(lost_device.id) not in json.dumps(state) and other_device.label not in json.dumps(state)
            status,cash=request(f"shops/{shop.id}/cash/")
            assert status==200 and cash["restoration"]["missing_data"]==(missing_count>0) and cash["totals_by_method_fcfa"]=={},(status,cash)
        assert request(known_path+"integrate/",data=known_command)==(200,known_receipt)
        status,captured=request(recovery+"journals/",data=journal(late_entry))
        assert status==201 and captured["new_operation_count"]==1 and captured["business_effects_created"]==0,(status,captured)
        late_path=recovery+"operations/"+captured["operations"][0]["capture_id"]+"/"
        status,late_preview=request(late_path+"integration-preview/")
        assert status==200 and not late_preview["can_apply"] and not late_preview["can_hold"],(status,late_preview)
        assert "recovery_closed_review_required" in {row["code"] for row in late_preview["issues"]}
        assert request(late_path+"integrate/",data={"operation_id":str(uuid.uuid4()),"previous_version":late_preview["version"],
            "preview_token":late_preview["preview_token"],"reason":"Aucune réouverture ni rapprochement autorisés implicitement"})[0]==409
        assert request(late_path+"review/",data={"operation_id":str(uuid.uuid4()),"previous_version":late_preview["review_version"],
            "preview_token":late_preview["preview_token"],"reason":"Pas de réservation automatique après clôture","state":"pending"})[0]==409
        state=request(prefix+"products/")[1]["restoration"]
        assert state["missing_data"] and state["late_operations"]==1 and state["late_review_required"]
        assert request(f"shops/{shops[1].id}/products/")[1]["restoration"]["late_operations"]==0
        assert request(prefix+"cash/")[1]["totals_by_method_fcfa"]=={}
        assert int(request(prefix+"products/")[1]["results"][0]["stock_available_milli"])==5000
        assert request(closure_path+"closures/",technical_token,closure_command)==(200,closed)
        assert request(known_path+"integrate/",data=known_command)==(200,known_receipt)
        assert request(f"shops/{shops[1].id}/recovery/operations/{captured['operations'][0]['capture_id']}/integration-preview/")[0]==404
        assert (RecoveryClosure.objects.count(),RecoveryJournalOperation.objects.count(),RecoveryIntegrationDecision.objects.count())==(1,2,1)
        assert (Sale.objects.count(),CashMovement.objects.count(),StockReservation.objects.count(),RecoverySaleHold.objects.count())==(0,0,0,0)
        assert StockMovement.objects.count()==2
        DeviceSession.objects.filter(id=technical_session.id).update(revoked_at=timezone.now())
        assert request(closure_path+"closures/",technical_token,closure_command)[0]==401
    result={"recovery_partial_closure_wsgi":"passed","shops":2,"native_late_sale_fcfa":500,
        "private_sqlite_baseline_replacement":True,"technical_preview_closure_exact_retry":True,
        "complete_closure_refused_without_proof":True,"current_technical_session_rechecked":True,
        "missing_warning_scoped_to_a":True,"late_journal_from_revoked_installation_preserved":True,
        "late_operations_a":1,"late_operations_b":0,"late_sale_integration_blocked":True,
        "late_pending_reservation_blocked":True,"original_integration_decision_retry_preserved":True,
        "stock_a_milli":5000,"stock_b_milli":7000,"cash_effect_fcfa":0,"pending_reservations":0,
        "production_restore_activation_exercised":False,"otp_exercised":False}
    (root/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False))


if __name__=="__main__":
    main()
