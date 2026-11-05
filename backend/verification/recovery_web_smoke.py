"""Exercise Unfold login and scoped decisions under maintenance on private SQLite."""
from datetime import timedelta
from html.parser import HTMLParser
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import build_opener,HTTPCookieProcessor,Request
import uuid
from wsgiref.simple_server import make_server,WSGIRequestHandler


class QuietHandler(WSGIRequestHandler):
    def log_message(self,format,*args):
        pass


class HiddenFields(HTMLParser):
    def __init__(self,html):
        super().__init__()
        self.fields={}
        self.feed(html)
    def handle_starttag(self,tag,attrs):
        attributes=dict(attrs)
        if tag=="input" and attributes.get("type")=="hidden" and attributes.get("name"):
            self.fields[attributes["name"]]=attributes.get("value","")


def main():
    with TemporaryDirectory(prefix="recovery-web-http-") as directory:
        root=Path(directory)
        database,gate,vault=root/"source.sqlite3",root/"gate.sqlite3",root/"external"/"proof.sqlite3"
        os.environ["DJANGO_SETTINGS_MODULE"]="config.test_settings"
        os.environ["APP_DB_PATH"]=str(database)
        import django
        django.setup()
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.db import connections
        from django.test import override_settings
        from django.utils import timezone
        from backups.models import RestoreAccessDecision,RestoreAccessReview,RecoveryDevice
        from backups.services.restore_gate import initialize_gate,enter_maintenance,read_gate
        from backups.services.restore_quarantine import _suspend
        from backups.services.technical_recovery import provision_technical_recovery
        from identity.models import User,Shop,Installation

        call_command("migrate",verbosity=0)
        old=User.objects.create_superuser(username="archived-technical",password="old-fixture-password-42!")
        owner=User.objects.create_user(username="restored-owner")
        Installation.objects.create(user=owner,label="Appareil archivé de l’équipe")
        shops=[Shop.objects.create(owner=owner,name=name) for name in ("Boutique A","Boutique B")]
        initialize_gate(gate,database,operation_id=uuid.uuid4(),requested_by=old.id,reason="Fixture privée")
        code=provision_technical_recovery(gate,vault,database,root/"archives",operation_id=uuid.uuid4(),
            requested_by=old.id,reason="Preuve préparée avant incident simulé",gate_version=1)
        enter_maintenance(gate,operation_id=uuid.uuid4(),requested_by=old.id,reason="Incident simulé",expected_version=1)
        connections.close_all()
        _suspend(database,timezone.now().replace(tzinfo=None).isoformat(sep=" "),generation_id=uuid.uuid4(),
            operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),requested_by=old.id)
        operation=uuid.uuid4()
        password="recovery-web-fixture-new-82!"
        script=Path(__file__).resolve().parents[2]/"tools/technical_recovery.py"
        completed=subprocess.run([sys.executable,"-I","-X","utf8",str(script),"bootstrap",
            "--gate",str(gate),"--vault",str(vault),"--database",str(database),"--operation-id",str(operation),
            "--requested-by",str(old.id),"--reason","Accès technique privé neuf","--gate-version","2",
            "--username","recovered-technical","--stdin-json"],text=True,capture_output=True,timeout=60,
            input=json.dumps({"code":code.secret,"password":password,"password_confirmation":password}))
        assert completed.returncode==0,"Bootstrap failed"
        assert code.secret not in completed.stdout+completed.stderr and password not in completed.stdout+completed.stderr
        receipt=json.loads(completed.stdout)
        options={"vault":vault,"bootstrap_operation":str(operation),"gate_version":2}
        with override_settings(RESTORE_GATE_PATH=gate,RESTORE_RECOVERY=options,BACKUP_ROOT=root/"archives",
                PASSWORD_HASHERS=["django.contrib.auth.hashers.PBKDF2PasswordHasher"]):
            server=make_server("127.0.0.1",0,get_wsgi_application(),handler_class=QuietHandler)
            thread=Thread(target=server.serve_forever,daemon=True)
            thread.start()
            origin=f"http://127.0.0.1:{server.server_port}"
            cookies=CookieJar()
            browser=build_opener(HTTPCookieProcessor(cookies))
            def request(path,body=None):
                req=Request(origin+path,data=None if body is None else urlencode(body).encode(),
                    headers={"Accept-Language":"fr"})
                try:
                    response=browser.open(req,timeout=30)
                except HTTPError as error:
                    response=error
                with response:
                    return response.status,response.geturl().removeprefix(origin),response.read().decode("utf-8")
            try:
                assert request("/api/v1/shops/")[0]==503
                assert request("/admin/core/user/")[0]==503
                status,path,html=request("/admin/login/")
                assert status==200
                csrf=HiddenFields(html).fields["csrfmiddlewaretoken"]
                bad=request("/admin/login/",{"username":"archived-technical","password":"old-fixture-password-42!",
                    "csrfmiddlewaretoken":csrf,"next":"/admin/"})
                assert bad[0]==200 and bad[1]=="/admin/login/"
                csrf=HiddenFields(bad[2]).fields["csrfmiddlewaretoken"]
                status,path,html=request("/admin/login/",{"username":"recovered-technical","password":password,
                    "csrfmiddlewaretoken":csrf,"next":"/admin/"})
                assert status==200 and path=="/admin/backups/restoreaccessreview/","Recovery login did not reach its queue"
                reviews=list(RestoreAccessReview.objects.filter(generation_id=receipt["restore_generation_id"]).order_by("shop__name"))
                assert len(reviews)==2 and all(str(review.id) in html for review in reviews)
                url=f"/admin/backups/restoreaccessreview/{reviews[0].id}/control/"
                status,path,html=request(url)
                assert status==200 and "Contrôler les accès restaurés" in html
                fields=HiddenFields(html).fields
                body={**fields,"state":"confirmed","reason":"Identité et qualité actuelles contrôlées","evidence_ref":""}
                without_csrf={key:value for key,value in body.items() if key!="csrfmiddlewaretoken"}
                assert request(url,without_csrf)[0]==403
                first=request(url,body)
                assert first[0]==200 and "Décision enregistrée" in first[2]
                sessions=request("/admin/backups/restoresession/")
                devices=request("/admin/backups/recoverydevice/")
                assert sessions[0]==200 and "Sessions de récupération après restauration" in sessions[2]
                assert devices[0]==200 and "Appareil archivé de l’équipe" in devices[2]
                assert "ne confirment ni la récupération" in devices[2]
                observations=list(RecoveryDevice.objects.filter(session__generation_id=receipt["restore_generation_id"]))
                assert len(observations)==2 and len({row.origin_installation_id for row in observations})==1
                assert request("/admin/backups/recoverydevice/",{"action":"delete_selected","_selected_action":str(observations[0].id)})[0]==503
                assert request(f"/admin/backups/recoverydevice/{observations[0].id}/change/")[0]==503
                assert RecoveryDevice.objects.count()==2
                assert request(url,body)[1]==first[1]
                assert RestoreAccessDecision.objects.count()==1
                decision=RestoreAccessDecision.objects.get()
                assert str(decision.requested_by_id)==receipt["user_id"] and decision.evidence_ref==""
                assert not RestoreAccessDecision.objects.filter(review=reviews[1]).exists()
                stale=request(url,{**body,"operation_id":str(uuid.uuid4())})
                assert stale[0]==409
                status,path,html=request(url)
                refusal={**HiddenFields(html).fields,"state":"refused","reason":"Contrôle actualisé non concluant","evidence_ref":""}
                assert request(url,refusal)[0]==200
                assert RestoreAccessDecision.objects.count()==2
                assert RestoreAccessDecision.objects.get(id=decision.id).state=="confirmed"
                assert request("/api/v1/shops/")[0]==503
                assert request("/admin/backups/backupjob/")[0]==503
                assert read_gate(gate)["mode"]=="maintenance"
                print(json.dumps({"recovery_unfold_http":"passed","shops":2,"external_bootstrap_bound":True,
                    "fresh_technical_login":True,"archived_technical_refused":True,"csrf_enforced":True,
                    "scoped_confirmation":True,"replay_without_duplicate":True,"stale_version_409":True,
                    "refusal_preserves_history":True,"business_and_other_admin_still_503":True,
                    "recovery_inventory_lists":True,"inventory_writes_503":True,"generic_inventory_edit_503":True,
                    "one_phone_two_shop_observations":True,
                    "gate_still_closed":True,"otp_exercised":False,"production_activation":False}))
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
                connections.close_all()


if __name__=="__main__":
    main()
