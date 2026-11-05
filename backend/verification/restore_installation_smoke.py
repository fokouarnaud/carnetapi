"""Real HTTP and console replacement in a disposable two-shop installation."""
from contextlib import closing,contextmanager
from datetime import timedelta
from http.cookiejar import CookieJar
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request,urlopen,build_opener,HTTPCookieProcessor
import uuid
from wsgiref.simple_server import make_server,WSGIRequestHandler
from verification.recovery_web_smoke import HiddenFields
from verification.customer_recovery_flow import record_customer_flow,recover_customer_flow


class QuietHandler(WSGIRequestHandler):
    def log_message(self,*args):
        pass


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def disposable_installation_directory():
    """Close SQLite connections before deleting even an early failed fixture."""
    with TemporaryDirectory(prefix="restore-install-http-") as directory:
        try:
            yield directory
        finally:
            from django.conf import settings
            if settings.configured:
                from django.db import connections
                connections.close_all()


def main(*,return_settlements=False,lost_returns=False,physical_returns=False,non_sellable_decisions=False,sale_cancellations=False,supplier_dependencies=False,supplier_payments=False,supplier_invoice_in_t0=False,supplier_documents=False,purchase_receipts=False,transfer_receipts=False):
    if transfer_receipts and any((return_settlements,lost_returns,physical_returns,non_sellable_decisions,sale_cancellations,supplier_dependencies,supplier_payments,supplier_invoice_in_t0,supplier_documents,purchase_receipts)):
        raise ValueError('Transfer receipt verification requires its own surviving-departure baseline.')
    if purchase_receipts and not supplier_documents:
        raise ValueError('Physical purchase receipt verification requires the lost purchase document baseline.')
    if supplier_documents and (not supplier_payments or supplier_invoice_in_t0):
        raise ValueError('Lost supplier documents require the supplier payment empty T0 baseline.')
    if supplier_invoice_in_t0 and not supplier_payments:
        raise ValueError('Retained supplier invoice verification requires supplier payments.')
    if supplier_payments and not supplier_dependencies:
        raise ValueError('Supplier-payment verification requires the supplier empty T0 baseline.')
    if supplier_dependencies and any((return_settlements,lost_returns,physical_returns,non_sellable_decisions,sale_cancellations)):
        raise ValueError('Supplier-dependency verification has its own empty T0 baseline.')
    if sale_cancellations and (not (return_settlements and lost_returns) or physical_returns or non_sellable_decisions):
        raise ValueError('Cancellation verification requires the lost-return baseline and its own replay scenario.')
    if non_sellable_decisions and not physical_returns:
        raise ValueError('Disposition verification requires physical-return recovery.')
    if physical_returns and not (return_settlements and lost_returns):
        raise ValueError('Physical-return verification requires its lost commercial-return scenario.')
    with disposable_installation_directory() as directory:
        root=Path(directory)
        database,gate=root/"business.sqlite3",root/"gate.sqlite3"
        vault=root/"external"/"proof.sqlite3"
        os.environ["DJANGO_SETTINGS_MODULE"]="config.test_settings"
        os.environ["APP_DB_PATH"]=str(database)
        import django
        django.setup()
        from cryptography.fernet import Fernet
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.db import connections
        from django.db.migrations.loader import MigrationLoader
        from django.test import override_settings
        from django.utils import timezone
        from PIL import Image
        from rest_framework_simplejwt.tokens import AccessToken
        from backups.services.archive import build_archive
        from backups.services.restore_preparation import prepare_restore
        from backups.services.restore_quarantine import quarantine_restore
        from backups.services.restore_gate import initialize_gate,enter_maintenance,read_gate,abort_maintenance,RestoreGateError,acquire_request
        from backups.services.technical_recovery import provision_technical_recovery
        from catalogue.models import Product,ProductPhoto
        from identity.models import User,Shop,Installation,DeviceSession,Membership,ShopDeviceAccess
        call_command("migrate",verbosity=0)
        owner=User.objects.create_user(username="restore-http-owner")
        operator=User.objects.create_superuser(username="restore-http-technical",password=None)
        shops=[Shop.objects.create(owner=owner,name=name) for name in ("A","B")]
        seller=User.objects.create_user(username="restore-http-seller")
        Membership.objects.create(shop=shops[0],user=seller)
        Installation.objects.create(user=seller,label="Vendeur archivé")
        product=Product.objects.create(shop=shops[0],name="Produit conservé",unit_kind="piece",unit_label="pièce",sale_price_fcfa=500)
        content=BytesIO()
        Image.new("RGB",(24,24),"blue").save(content,format="JPEG")
        photo=content.getvalue()
        ProductPhoto.objects.create(product=product,data=photo,width=24,height=24)
        installation=Installation.objects.create(user=owner)
        session=DeviceSession.objects.create(installation=installation,expires_at=timezone.now()+timedelta(days=1))
        for shop in shops:ShopDeviceAccess.objects.create(shop=shop,installation=installation,verified_at=timezone.now(),offline_expires_at=timezone.now()+timedelta(days=7))
        return_baseline=None;supplier_money_baseline=None;supplier_document_catalogue=None;transfer_baseline=None
        if transfer_receipts:
            from verification.transfer_receipt_recovery_flow import (prepare_transfer_baseline,record_transfer_receipt_flow,
                recover_transfer_receipt_flow,audit_transfer_receipt_t0,audit_transfer_receipt_flow)
            transfer_baseline=prepare_transfer_baseline(source_shop=shops[1],destination_shop=shops[0],owner=owner,
                installation=installation,session=session,product=product)
        if supplier_documents:
            from catalogue.models import ProductPack
            pack=ProductPack.objects.create(product=product,name='Carton de 24',base_quantity_milli=24000,sale_price_fcfa=2400)
            supplier_document_catalogue={'product_id':str(product.id),'pack_id':str(pack.id)}
        if return_settlements:
            from verification.return_settlement_recovery_flow import prepare_return_baseline,record_return_flow,recover_return_flow,audit_return_t0,audit_return_flow,record_lost_return_flow,recover_lost_return_flow
            return_baseline=prepare_return_baseline(shop=shops[0],owner=owner,installation=installation,session=session,product=product,lost_returns=lost_returns)
        if physical_returns:
            from verification.physical_return_recovery_flow import record_physical_return_flow,recover_physical_return_flow
        if sale_cancellations:
            from verification.return_settlement_recovery_flow import record_cancellation_flow,recover_cancellation_flow
        if supplier_dependencies:
            from verification.supplier_dependency_recovery_flow import record_supplier_flow,recover_supplier_flow,audit_supplier_t0,audit_supplier_flow
            if supplier_payments:
                from verification.supplier_payment_recovery_flow import record_supplier_money_flow as record_supplier_flow,recover_supplier_money_flow as recover_supplier_flow,audit_supplier_money_flow as audit_supplier_flow,audit_supplier_money_t0 as audit_supplier_t0,prepare_supplier_invoice_baseline
                if supplier_invoice_in_t0:supplier_money_baseline=prepare_supplier_invoice_baseline(shop=shops[0],owner=owner,session=session,product=product)
                if purchase_receipts:
                    from verification.purchase_receipt_recovery_flow import record_purchase_receipt_flow as record_supplier_flow,recover_purchase_receipt_flow as recover_supplier_flow,audit_purchase_receipt_flow as audit_supplier_flow,audit_purchase_receipt_t0 as audit_supplier_t0
        if non_sellable_decisions:
            from verification.unsellable_decision_recovery_flow import prepare_supplier,record_disposition_flow,recover_disposition_flow
            return_baseline['supplier']=prepare_supplier(shop=shops[0],owner=owner,session=session)
        archive_key=Fernet.generate_key()
        archive=build_archive(database,root/"parts",archive_key,backup_id=uuid.uuid4(),key_id="test")
        prepared=prepare_restore([root/"parts"/p["filename"] for p in archive.parts],archive_key,root/"prepared",
            expected_migrations=sorted([list(row) for row in MigrationLoader(None).disk_migrations]))
        quarantined=quarantine_restore(prepared.path.parent,root/"quarantined",operation_id=uuid.uuid4(),
            requested_by=operator.id,reason="Suspendre les accès archivés")
        initialize_gate(gate,database,operation_id=uuid.uuid4(),requested_by=operator.id,reason="Installation privée")
        issued=provision_technical_recovery(gate,vault,database,root/"archives",operation_id=uuid.uuid4(),
            requested_by=operator.id,reason="Preuve de secours préparée avant l'incident",gate_version=1)
        connections.close_all()
        with override_settings(RESTORE_GATE_PATH=gate,RESTORE_RECOVERY=None,BACKUP_ROOT=root/"archives"):
            server=make_server("127.0.0.1",0,get_wsgi_application(),handler_class=QuietHandler)
            thread=Thread(target=server.serve_forever,daemon=True)
            thread.start()
            base=f"http://127.0.0.1:{server.server_port}"
            token=AccessToken.for_user(owner);token["session_id"]=str(session.id)
            def request(path,credential=None,data=None,method=None):
                try:
                    response=urlopen(Request(base+path,data=None if data is None else json.dumps(data).encode(),method=method,
                        headers={"Authorization":"Bearer "+str(token if credential is None else credential),"Content-Type":"application/json"}),timeout=15)
                except HTTPError as error:
                    response=error
                with response:
                    return response.status,json.loads(response.read()),dict(response.headers)
            try:
                before,data,_headers=request("/api/v1/shops/")
                assert before==200 and data["count"]==2
                # T0 is already archived. Keep these T1 HTTP receipts in this
                # process, independently from the business database replaced below.
                record_flow=record_transfer_receipt_flow if transfer_receipts else record_supplier_flow if supplier_dependencies else record_cancellation_flow if sale_cancellations else record_disposition_flow if non_sellable_decisions else record_physical_return_flow if physical_returns else record_lost_return_flow if lost_returns else record_return_flow if return_settlements else record_customer_flow
                flow_values={'shop_id':shops[0].id,'actor_id':owner.id,'installation_id':installation.id}
                if transfer_receipts:survivor=record_flow(request,**flow_values,baseline=transfer_baseline)
                elif return_settlements:survivor=record_flow(request,**flow_values,baseline=return_baseline)
                elif supplier_payments:survivor=record_flow(request,**flow_values,baseline=supplier_money_baseline,documents=supplier_document_catalogue)
                else:survivor=record_flow(request,**flow_values)
                enter_maintenance(gate,operation_id=uuid.uuid4(),requested_by=operator.id,
                    reason="Remplacer depuis la copie vérifiée",expected_version=1,timeout=10)
                original=digest(database)
                tool=Path(__file__).resolve().parents[2]/"tools/install_server_restore.py"
                command=[sys.executable,"-I","-X","utf8",str(tool),str(quarantined.path.parent),
                    "--gate",str(gate),"--database",str(database),"--operation-id",str(uuid.uuid4()),
                    "--requested-by",str(operator.id),"--reason","Installer la copie privée","--expected-version","2"]
                environment={**os.environ,"DJANGO_SETTINGS_MODULE":"intentionally.invalid",
                    "APP_DB_PATH":str(root/"trap.sqlite3"),"APP_EMAIL_CONFIG_FILE":str(root/"missing-mail.json")}
                completed=subprocess.run(command,text=True,capture_output=True,timeout=60,env=environment)
                assert completed.returncode==0,completed.stderr
                receipt=json.loads(completed.stdout)
                assert receipt["activation_allowed"] is False and receipt["live_database_replaced"] is True
                assert digest(database)==quarantined.manifest["candidate_sha256"]
                assert digest(Path(receipt["rollback_directory"])/"original.sqlite3")==original
                assert not (root/"trap.sqlite3").exists()
                after,body,headers=request("/api/v1/shops/")
                assert after==503 and body["code"]=="service_unavailable" and headers["Cache-Control"]=="no-store"
                assert request("/admin/login/")[0]==503
                with closing(sqlite3.connect(database.as_uri()+"?mode=ro",uri=True)) as db:
                    assert db.execute("SELECT COUNT(*) FROM core_shop").fetchone()[0]==2
                    assert db.execute("SELECT COUNT(*) FROM core_user WHERE is_active OR is_staff OR is_superuser").fetchone()[0]==0
                    assert db.execute("SELECT data FROM core_productphoto WHERE product_id=?",[product.id.hex]).fetchone()[0]==photo
                    assert db.execute("SELECT revoked_at FROM core_devicesession WHERE id=?",[session.id.hex]).fetchone()[0] is not None
                    if transfer_receipts:audit_transfer_receipt_t0(db,survivor)
                    elif supplier_payments:audit_supplier_t0(db,survivor)
                    elif supplier_dependencies:audit_supplier_t0(db)
                    elif return_settlements:audit_return_t0(db,return_baseline,lost_returns=lost_returns)
                    else:
                        for table in ("core_customer","core_customerdebt","core_customerpayment","core_cashmovement"):
                            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]==0,table
                replay=subprocess.run(command,text=True,capture_output=True,timeout=60,env=environment)
                assert replay.returncode==0 and json.loads(replay.stdout)==receipt
                try:
                    abort_maintenance(gate,operation_id=uuid.uuid4(),requested_by=operator.id,
                        reason="Aucune activation implicite",expected_version=3)
                except RestoreGateError:
                    pass
                else:
                    raise AssertionError("Installation must not reopen through abort")
                state=read_gate(gate)
                assert state["mode"]=="maintenance" and state["installation"]["state"]=="installed"
                tools=Path(__file__).resolve().parents[2]/"tools"
                bootstrap_operation=uuid.uuid4()
                password="private-restoration-technical-83!"
                bootstrap=subprocess.run([sys.executable,"-I","-X","utf8",str(tools/"technical_recovery.py"),"bootstrap",
                    "--gate",str(gate),"--database",str(database),"--vault",str(vault),"--operation-id",str(bootstrap_operation),
                    "--requested-by",str(operator.id),"--reason","Accès technique actuel de la fixture","--gate-version","3",
                    "--username","restored-technical","--stdin-json"],input=json.dumps({"code":issued.secret,"password":password,
                        "password_confirmation":password}),text=True,capture_output=True,timeout=60,env=environment)
                assert issued.secret not in bootstrap.stdout+bootstrap.stderr and password not in bootstrap.stdout+bootstrap.stderr
                assert bootstrap.returncode==0,"Private technical bootstrap failed"
                bootstrap_receipt=json.loads(bootstrap.stdout)
                cookies=CookieJar();browser=build_opener(HTTPCookieProcessor(cookies))
                def web(path,body=None):
                    req=Request(base+path,data=None if body is None else urlencode(body).encode(),headers={"Accept-Language":"fr"})
                    try:response=browser.open(req,timeout=30)
                    except HTTPError as error:response=error
                    with response:return response.status,response.geturl().removeprefix(base),response.read().decode("utf-8")
                options={"vault":vault,"bootstrap_operation":str(bootstrap_operation),"gate_version":3}
                with override_settings(RESTORE_RECOVERY=options,PASSWORD_HASHERS=["django.contrib.auth.hashers.PBKDF2PasswordHasher"]):
                    status,_path,html=web("/admin/login/")
                    assert status==200
                    status,path,html=web("/admin/login/",{"username":"restored-technical","password":password,
                        "csrfmiddlewaretoken":HiddenFields(html).fields["csrfmiddlewaretoken"],"next":"/admin/"})
                    assert status==200 and path=="/admin/backups/restoreaccessreview/"
                    with closing(sqlite3.connect(database.as_uri()+"?mode=ro",uri=True)) as db:
                        review=db.execute("SELECT id FROM backups_restoreaccessreview WHERE shop_id=? AND user_id=?",[shops[0].id.hex,owner.id.hex]).fetchone()[0]
                    url=f"/admin/backups/restoreaccessreview/{uuid.UUID(review)}/control/"
                    status,_path,html=web(url);assert status==200
                    body={**HiddenFields(html).fields,"state":"confirmed","reason":"Identité et qualité actuelles vérifiées pour A","evidence_ref":""}
                    assert web(url,{key:value for key,value in body.items() if key!="csrfmiddlewaretoken"})[0]==403
                    confirmed=web(url,body)
                    assert confirmed[0]==200 and "Décision enregistrée" in confirmed[2]
                    assert request("/api/v1/shops/")[0]==503
                    activation_operation=uuid.uuid4()
                    activation=[sys.executable,"-I","-X","utf8",str(tools/"activate_server_restore.py"),
                        "--gate",str(gate),"--database",str(database),"--vault",str(vault),"--backup-root",str(root/"archives"),
                        "--installation-id",receipt["operation_id"],"--bootstrap-operation",str(bootstrap_operation),
                        "--operation-id",str(activation_operation),"--expected-version","3","--username","restored-technical",
                        "--reason","Copie installée et accès actuels contrôlés","--acknowledge-provisional","--stdin-json"]
                    opened=subprocess.run(activation,input=json.dumps({"password":password}),text=True,capture_output=True,timeout=60,env=environment)
                    assert password not in opened.stdout+opened.stderr
                    assert opened.returncode==0,"Private activation failed"
                    activation_receipt=json.loads(opened.stdout)
                    assert activation_receipt["mode"]=="open" and activation_receipt["version"]==4
                    assert activation_receipt["recovery_closed"] is False and activation_receipt["archived_permissions_restored"] is False
                    assert activation_receipt["requested_by"]==bootstrap_receipt["user_id"]
                    stale,stale_body,_headers=request("/api/v1/shops/")
                    assert stale==401 and stale_body["code"]=="invalid_session"
                    # A fixture fresh session exercises current server guards.
                    # It is not an OTP/login qualification.
                    with acquire_request(gate,database):
                        try:
                            current_owner=User.objects.get(id=owner.id)
                            fresh_installation=Installation.objects.create(user=current_owner,label="Fixture authentifiée après restauration")
                            fresh_session=DeviceSession.objects.create(installation=fresh_installation,expires_at=timezone.now()+timedelta(days=1))
                            fresh_token=AccessToken.for_user(current_owner);fresh_token["session_id"]=str(fresh_session.id)
                        finally:connections.close_all()
                    status,scoped,_headers=request("/api/v1/shops/",fresh_token)
                    assert status==200 and scoped["count"]==1 and scoped["results"][0]["id"]==str(shops[0].id)
                    status,products,_headers=request(f"/api/v1/shops/{shops[0].id}/products/",fresh_token)
                    assert status==200 and products["count"]==1 and products["results"][0]["id"]==str(product.id)
                    assert products["restoration"]["state"]=="recovering" and products["restoration"]["known_devices"]==2
                    assert request(f"/api/v1/shops/{shops[1].id}/products/",fresh_token)[0]==404
                    assert request(f"/api/v1/shops/{shops[0].id}/verify-access/",fresh_token,{})[0]==200
                    assert request(f"/api/v1/shops/{shops[1].id}/verify-access/",fresh_token,{})[0]==404
                    new_product={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"name":"Après réouverture contrôlée",
                        "unit_kind":"piece","unit_label":"pièce","sale_price_fcfa":1000,"purchase_price_fcfa":None}
                    product_path=f"/api/v1/shops/{shops[0].id}/products/"
                    assert request(product_path,fresh_token,new_product)[0]==201
                    assert request(product_path,fresh_token,new_product)[0]==200
                    denied={**new_product,"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4())}
                    assert request(f"/api/v1/shops/{shops[1].id}/products/",fresh_token,denied)[0]==404
                    recover_flow=recover_transfer_receipt_flow if transfer_receipts else recover_supplier_flow if supplier_dependencies else recover_cancellation_flow if sale_cancellations else recover_disposition_flow if non_sellable_decisions else recover_physical_return_flow if physical_returns else recover_lost_return_flow if lost_returns else recover_return_flow if return_settlements else recover_customer_flow
                    recovered_flow=recover_flow(request,shop_id=shops[0].id,foreign_shop_id=shops[1].id,
                        session_id=products["restoration"]["session_id"],credential=fresh_token,survivor=survivor)
                    same=subprocess.run(activation,input=json.dumps({"password":password}),text=True,capture_output=True,timeout=60,env=environment)
                    assert same.returncode==0 and json.loads(same.stdout)==activation_receipt
                    assert read_gate(gate)["version"]==4
                    current_digest=digest(database)
                    reinstall=subprocess.run(command,text=True,capture_output=True,timeout=60,env=environment)
                    assert reinstall.returncode!=0 and digest(database)==current_digest
                    assert request(product_path+new_product["id"]+"/",fresh_token)[0]==200
                with closing(sqlite3.connect(database.as_uri()+"?mode=ro",uri=True)) as db:
                    assert db.execute("SELECT is_active,is_staff,is_superuser FROM core_user WHERE id=?",[operator.id.hex]).fetchone()==(0,0,0)
                    assert db.execute("SELECT is_active FROM core_user WHERE id=?",[seller.id.hex]).fetchone()[0]==0
                    assert db.execute("SELECT COUNT(*) FROM core_membership WHERE is_active=1").fetchone()[0]==0
                    assert db.execute("SELECT COUNT(*) FROM backups_restoreaccessdecision").fetchone()[0]==1
                    assert db.execute("SELECT COUNT(*) FROM core_product WHERE id=?",[uuid.UUID(new_product["id"]).hex]).fetchone()[0]==1
                    assert db.execute("SELECT COUNT(*) FROM core_product WHERE id=?",[uuid.UUID(denied["id"]).hex]).fetchone()[0]==0
                    if transfer_receipts:audit_transfer_receipt_flow(db,survivor)
                    elif supplier_dependencies:audit_supplier_flow(db,survivor)
                    elif return_settlements:audit_return_flow(db,return_baseline,lost_returns=lost_returns,physical_returns=physical_returns,
                        non_sellable_decisions=non_sellable_decisions,sale_cancellations=sale_cancellations)
                    else:
                        assert db.execute("SELECT COUNT(*),SUM(amount_fcfa) FROM core_cashmovement").fetchone()==(2,2500)
                        assert db.execute("SELECT COUNT(*),SUM(remaining_fcfa) FROM core_customerdebt").fetchone()==(1,5600)
                        assert db.execute("SELECT COUNT(*),SUM(unallocated_fcfa) FROM core_customerpayment").fetchone()==(1,100)
                        assert db.execute("SELECT COUNT(*) FROM core_paymentregularization").fetchone()[0]==2
                        assert db.execute("SELECT COUNT(*),SUM(amount_fcfa) FROM core_regularizationallocation").fetchone()==(2,400)
                        assert db.execute("SELECT COUNT(*) FROM core_customerledgerentry").fetchone()[0]==5
                        assert db.execute("SELECT COUNT(*) FROM backups_recoveredoperationorigin").fetchone()[0]==7
                        assert db.execute("SELECT COUNT(*) FROM backups_recoveryintegrationdecision").fetchone()[0]==14
                        assert db.execute("SELECT COUNT(*) FROM core_sale").fetchone()[0]==0
                        assert db.execute("SELECT COUNT(*) FROM core_stockmovement").fetchone()[0]==0
                print(json.dumps({"restore_installation_http":"passed","shops":2,"http_before":before,"http_after":after,
                    "console_independent_of_deployment_settings":True,"original_preserved":True,"photo_and_uuids_preserved":True,
                    "archived_accounts_and_session_suspended":True,"replay_without_second_replacement":True,
                    "maintenance_until_explicit_activation":True,"business_activation_exercised":True,"rollback_exercised":False,
                    "fresh_technical_login":True,"csrf_enforced":True,"gate_open_version":4,"stale_session":401,
                    "confirmed_shop_accessible":True,"unconfirmed_shop":404,"sellers_still_suspended":True,
                    "recovery_still_provisional":True,"activation_replay_after_fresh_session":True,"reinstall_after_open_refused":True,
                    "current_owner_write_and_replay":True,"unconfirmed_shop_write_refused":True,"scoped_access_verification":True,
                    "otp_exercised":False,"external_emails_sent":0,**recovered_flow}))
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
                connections.close_all()


if __name__=="__main__":
    main()
