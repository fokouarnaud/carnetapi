"""Real HTTP/Unfold/SMTP hosting flow using private SQLite and loopback only.

Uses fixture accounts, not OTP. Does not renew a host, deploy cron or send mail
outside this process. SMTP acceptance is observed, never inbox delivery.
"""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone as utc
from email import policy
from email.parser import BytesParser
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPCookieProcessor, Request, urlopen
import uuid
from wsgiref.simple_server import make_server, WSGIRequestHandler

from verification.smtp_test_server import LocalSMTPServer, LocalSMTPHandler
from verification.recovery_web_smoke import HiddenFields


class QuietHandler(WSGIRequestHandler):
    def log_message(self,format,*args):pass


INTERRUPTED_WORKER=r'''
import os,sys
from pathlib import Path
from datetime import datetime,timezone
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
import django
django.setup()
from django.test import override_settings
from backups.models import HostingReminderAttempt
from backups.services.hosting import hosting_tick
real_save=HostingReminderAttempt.save
def stop_after_smtp(self,*args,**kwargs):
 if kwargs.get('update_fields')==['state','finished_at','failure_code','updated_at']:os._exit(73)
 return real_save(self,*args,**kwargs)
with override_settings(RESTORE_GATE_PATH=Path(sys.argv[2]),HOSTING_REMINDER_ACTOR_ID=sys.argv[3],
 DEFAULT_FROM_EMAIL='sender@example.test',EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend',
 EMAIL_HOST='127.0.0.1',EMAIL_PORT=int(sys.argv[4]),EMAIL_USE_TLS=False,EMAIL_USE_SSL=False,
 EMAIL_HOST_USER='',EMAIL_HOST_PASSWORD='',EMAIL_TIMEOUT=2),patch('django.utils.timezone.now',return_value=datetime(2026,12,3,tzinfo=timezone.utc)),patch.object(HostingReminderAttempt,'save',stop_after_smtp):
 hosting_tick()
raise AssertionError('The private worker did not reach its interruption point')
'''


REPAIR_AT_FIXTURE_TIME=r'''
import importlib.util,sys
from datetime import datetime,timezone
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('fixture_hosting_repair',sys.argv[1])
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
with patch('django.utils.timezone.now',return_value=datetime(2026,12,3,tzinfo=timezone.utc)):
 raise SystemExit(module.main(sys.argv[2:]))
'''


def main():
    with TemporaryDirectory(prefix="hosting-http-") as directory,ExitStack() as resources:
        os.environ["DJANGO_SETTINGS_MODULE"]="config.test_settings"
        os.environ["APP_DB_PATH"]=str(Path(directory)/"private.sqlite3")
        import django
        django.setup()
        from django.core.management import call_command
        from django.contrib.auth.hashers import PBKDF2PasswordHasher
        from django.core.wsgi import get_wsgi_application
        from django.db import connections
        from django.test import override_settings
        from django.utils import timezone
        from rest_framework_simplejwt.tokens import AccessToken
        from backups.models import HostingConfigurationChange, HostingReminder, HostingReminderAttempt, HostingReminderRepair
        from backups.services.restore_gate import initialize_gate, read_gate
        from identity.models import User, Installation, DeviceSession
        resources.callback(connections.close_all)
        call_command("migrate",verbosity=0)
        operator=User.objects.create_superuser(username="private-hosting-operator",password="private-smoke-fixture-42!")
        operator.password=PBKDF2PasswordHasher().encode("private-smoke-fixture-42!",uuid.uuid4().hex);operator.save()
        gate=Path(directory)/"gate.sqlite3"
        initialize_gate(gate,Path(directory)/"private.sqlite3",operation_id=uuid.uuid4(),requested_by=operator.id,reason="Fixture privée")
        installation=Installation.objects.create(user=operator)
        session=DeviceSession.objects.create(installation=installation,expires_at=timezone.now()+timedelta(days=60))
        token=AccessToken.for_user(operator);token["session_id"]=str(session.id)
        smtp=LocalSMTPServer(("127.0.0.1",0),LocalSMTPHandler)
        smtp.mode,smtp.received="accept",[]
        mailthread=Thread(target=smtp.serve_forever,daemon=True);mailthread.start()
        resources.callback(smtp.server_close);resources.callback(mailthread.join,3);resources.callback(smtp.shutdown)
        resources.enter_context(override_settings(HOSTING_REMINDER_TOKEN="h"*48,HOSTING_REMINDER_ACTOR_ID=str(operator.id),
            RESTORE_GATE_PATH=gate,PASSWORD_HASHERS=["django.contrib.auth.hashers.PBKDF2PasswordHasher"],
            DEFAULT_FROM_EMAIL="sender@example.test",EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
            EMAIL_HOST="127.0.0.1",EMAIL_PORT=smtp.server_address[1],EMAIL_USE_TLS=False,EMAIL_USE_SSL=False,
            EMAIL_HOST_USER="",EMAIL_HOST_PASSWORD="",EMAIL_TIMEOUT=2))
        server=make_server("127.0.0.1",0,get_wsgi_application(),handler_class=QuietHandler)
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        resources.callback(server.server_close);resources.callback(thread.join,5);resources.callback(server.shutdown)
        origin=f"http://127.0.0.1:{server.server_port}"
        browser=build_opener(HTTPCookieProcessor(CookieJar()))

        def web(path,data=None,language="fr"):
            req=Request(origin+path,data=None if data is None else urlencode(data).encode(),headers={"Accept-Language":language})
            try:response=browser.open(req,timeout=30)
            except HTTPError as error:response=error
            with response:return response.status,response.geturl().removeprefix(origin),response.read().decode("utf-8")

        def api(path,data=None,method=None,trigger=False):
            req=Request(origin+"/api/v1/hosting/"+path,data=None if data is None else json.dumps(data).encode(),
                method=method,headers={"Authorization":"Bearer "+("h"*48 if trigger else str(token)),"Content-Type":"application/json"})
            try:response=urlopen(req,timeout=30)
            except HTTPError as error:response=error
            with response:return response.status,json.load(response)

        status,_,html=web("/admin/login/")
        assert status==200
        csrf=HiddenFields(html).fields["csrfmiddlewaretoken"]
        status,path,_=web("/admin/login/",{"username":operator.username,"password":"private-smoke-fixture-42!",
            "csrfmiddlewaretoken":csrf,"next":"/admin/"})
        assert status==200 and path=="/admin/"
        config="/admin/backups/hostingconfiguration/configure/"
        for language,label in (("fr","Hébergement et expiration"),("en","Hosting and expiration")):
            status,_,html=web(config,language=language)
            assert status==200 and label in html
        fields=HiddenFields(html).fields
        command={**fields,"enabled":"on","expires_at":"2026-11-10 15:00:00","recipient":"operator@example.test",
            "language":"fr","action":"verify_expiration","evidence_reference":"Console privée du fournisseur",
            "reason":"Date vérifiée manuellement"}
        with patch("django.utils.timezone.now",return_value=datetime(2026,11,1,tzinfo=utc.utc)):
            assert web(config,{key:value for key,value in command.items() if key!="csrfmiddlewaretoken"})[0]==403
            assert web(config,command)[0]==200
        assert len(smtp.received)==0
        before=api("configuration/")[1]
        assert before["version"]==1 and before["last_reported_renewal_at"] is None
        at=datetime(2026,11,3,tzinfo=utc.utc)
        with patch("django.utils.timezone.now",return_value=at):
            assert api("trigger/",{},trigger=True)==(200,{"status":"accepted"})
            assert api("trigger/",{},trigger=True)==(200,{"status":"idle"})
        assert len(smtp.received)==1
        message=BytesParser(policy=policy.default).parsebytes(smtp.received[0])
        assert message["To"]=="operator@example.test" and "7 jours" in message["Subject"]
        observation={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"observed_at":"2026-10-20T10:00:00Z",
            "installed_bytes":500000000,"quota_used_bytes":500,"quota_limit_bytes":500,"service_state":"down",
            "evidence_reference":"Mesure privée datée","reason":"Incident et espace déclarés"}
        assert api("observations/",observation)[0]==201
        assert api("observations/",observation)[0]==200
        assert api("status/")[1]["reported_space"]["installed_budget_state"]=="reported_limit_exceeded"
        smtp.mode="drop"
        with patch("django.utils.timezone.now",return_value=datetime(2026,11,8,tzinfo=utc.utc)):
            assert api("trigger/",{},trigger=True)==(200,{"status":"uncertain"})
            assert api("trigger/",{},trigger=True)==(200,{"status":"idle"})
            reminder=HostingReminder.objects.get(lead_days=2)
            retry={"operation_id":str(uuid.uuid4()),"expected_attempt_id":str(reminder.current_attempt_id),
                "possible_duplicate_confirmed":True,"reason":"Réponse SMTP perdue ; renvoi explicite"}
            retry_path=f"reminders/{reminder.id}/retry/"
            smtp.mode="accept"
            assert api(retry_path,retry)[0]==201
            assert api(retry_path,retry)[0]==200
            history=api(f"reminders/{reminder.id}/attempts/")[1]
            assert [item["state"] for item in history["results"]]==["uncertain","accepted"]
            assert history["results"][0]["message_id"]==history["results"][1]["message_id"]
        assert len(smtp.received)==3 and HostingReminderAttempt.objects.count()==3
        assert api("configuration/")[1]==before
        with patch("django.utils.timezone.now",return_value=datetime(2026,11,9,tzinfo=utc.utc)):
            status,_,html=web(config)
            assert status==200
            report={**HiddenFields(html).fields,**{key:value for key,value in command.items() if key not in fields},
                "operation_id":str(uuid.uuid4()),"expected_version":1,"action":"confirm_renewal","expires_at":"2026-12-10 15:00:00"}
            assert web(config,report)[0]==200
        current=api("configuration/")[1]
        assert current["version"]==2 and current["last_reported_renewal_at"] is not None
        assert HostingConfigurationChange.objects.count()==2 and len(smtp.received)==3
        backend=Path(__file__).resolve().parents[1]
        stopped=subprocess.run([sys.executable,"-I","-X","utf8","-c",INTERRUPTED_WORKER,str(backend),str(gate),str(operator.id),str(smtp.server_address[1])],
            capture_output=True,text=True,timeout=60)
        assert stopped.returncode==73,"Private SMTP worker did not stop at its durable boundary"
        assert len(smtp.received)==4
        interrupted=HostingReminder.objects.get(expires_at=current["expires_at"],lead_days=7)
        assert interrupted.state=="sending"
        interrupted_attempt=HostingReminderAttempt.objects.get(id=interrupted.current_attempt_id)
        assert interrupted_attempt.state=="sending" and interrupted_attempt.finished_at is None
        repair_operation=uuid.uuid4()
        repair_command=[sys.executable,"-I","-X","utf8","-c",REPAIR_AT_FIXTURE_TIME,str(backend.parent/"tools/repair_hosting_reminder.py"),
            "--gate",str(gate),"--database",str(Path(directory)/"private.sqlite3"),"--gate-version","1","--username",operator.username,
            "--attempt-id",str(interrupted_attempt.id),"--operation-id",str(repair_operation),"--reason","Arrêt constaté, acceptation SMTP inconnue",
            "--acknowledge-uncertain","--stdin-json"]
        repaired=subprocess.run(repair_command,input=json.dumps({"password":"private-smoke-fixture-42!"}),capture_output=True,text=True,timeout=60)
        assert repaired.returncode==0,"Private repair console failed"
        receipt=json.loads(repaired.stdout)
        assert receipt["after_state"]=="uncertain" and receipt["smtp_sent"] is False
        assert len(smtp.received)==4 and HostingReminderRepair.objects.count()==1
        assert read_gate(gate)["mode"]=="open" and read_gate(gate)["version"]==1
        with patch("django.utils.timezone.now",return_value=datetime(2026,12,3,tzinfo=utc.utc)):
            assert api("trigger/",{},trigger=True)==(200,{"status":"idle"})
            data={"operation_id":str(uuid.uuid4()),"expected_attempt_id":str(interrupted_attempt.id),
                "possible_duplicate_confirmed":True,"reason":"Renvoi manuel après examen de l'interruption"}
            retry_path=f"reminders/{interrupted.id}/retry/"
            assert api(retry_path,data)[0]==201
            assert api(retry_path,data)[0]==200
            history=api(f"reminders/{interrupted.id}/attempts/")[1]
            assert [item["state"] for item in history["results"]]==["uncertain","accepted"]
            assert history["results"][0]["message_id"]==history["results"][1]["message_id"]
        replay=subprocess.run(repair_command,input=json.dumps({"password":"private-smoke-fixture-42!"}),capture_output=True,text=True,timeout=60)
        assert replay.returncode==0 and json.loads(replay.stdout)==receipt
        assert len(smtp.received)==5 and HostingReminderAttempt.objects.count()==5 and HostingReminderRepair.objects.count()==1
        assert api("configuration/")[1]==current
        operator.is_superuser=False;operator.save(update_fields=["is_superuser"])
        assert api("status/")[0]==403 and web(config)[0]==403
        session.revoked_at=timezone.now();session.save(update_fields=["revoked_at"])
        assert api("status/")[0]==401
        print(json.dumps({"http":"passed","unfold":"fr_en_csrf_current_role_passed","smtp":"loopback_only",
            "smtp_messages":5,"j7":"accepted","j2":["uncertain","explicit_resend_accepted"],
            "process_interruption":{"exit":73,"repair":"uncertain","smtp_during_repair":0,"explicit_retry":"accepted"},
            "automatic_renewal":False,"manual_renewal_reports":1,"revoked_session":401}))


if __name__=="__main__":main()
