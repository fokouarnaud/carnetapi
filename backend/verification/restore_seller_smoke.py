"""Exercise seller revalidation over HTTP in an open-gate recovery-state fixture.

No restoration activation or OTP is simulated: current sessions are fixture data.
"""
from datetime import timedelta
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
    def log_message(self,format,*args):
        pass


def main():
    with TemporaryDirectory(prefix="restore-seller-http-") as directory:
        root=Path(directory)
        database,gate=root/"source.sqlite3",root/"gate.sqlite3"
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
        from backups.models import RestoreAccessGeneration,RestoreAccessReview,RestoreAccessDecision
        from backups.services.access_decisions import decide_responsible_access
        from backups.services.restore_gate import initialize_gate
        from common.models import Activity
        from identity.models import User,Shop,Membership,Installation,DeviceSession

        call_command("migrate",verbosity=0)
        technical=User.objects.create_superuser(username="seller-http-technical",password=None)
        owner=User.objects.create_user(username="seller-http-owner",is_active=False)
        seller=User.objects.create_user(username="seller-http-subject",is_active=False)
        shops=[Shop.objects.create(owner=owner,name=name) for name in ("A","B")]
        generation=RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),initiated_by=technical.id)
        flags=dict.fromkeys(("can_grant_credit","can_collect_debt","can_read_debt","can_prepare_reminder","can_read_cost","can_read_cash_balance"),True)
        for shop in shops:
            review=RestoreAccessReview.objects.create(generation=generation,shop=shop,user=owner,
                archived_role="owner",was_active=True,archived_permissions=flags)
            if shop==shops[0]:
                decide_responsible_access(actor=technical,review_id=review.id,operation_id=uuid.uuid4(),previous_version=0,
                    state="confirmed",reason="Responsable actuel de A contrôlé")
        members=[Membership.objects.create(user=seller,shop=shop,role="seller",is_active=False,**flags) for shop in shops]
        for member in members:
            RestoreAccessReview.objects.create(generation=generation,shop=member.shop,user=seller,membership=member,
                archived_role="seller",was_active=True,archived_permissions=flags)
        old_device=Installation.objects.create(user=seller,revoked_at=timezone.now())
        old_session=DeviceSession.objects.create(installation=old_device,expires_at=timezone.now()+timedelta(days=30),revoked_at=timezone.now())
        def token(user,old=None):
            if old is None:
                installation=Installation.objects.create(user=user)
                session=DeviceSession.objects.create(installation=installation,expires_at=timezone.now()+timedelta(days=30))
            else:
                session=old
            value=AccessToken()
            value["user_id"]=str(user.id);value["session_id"]=str(session.id)
            return str(value)
        owner_token=token(owner)
        old_token=token(seller,old_session)
        initialize_gate(gate,database,operation_id=uuid.uuid4(),requested_by=technical.id,
            reason="Barrière ouverte pour fixture de droits, sans activation de restauration")
        with override_settings(RESTORE_GATE_PATH=gate):
            server=make_server("127.0.0.1",0,get_wsgi_application(),handler_class=QuietHandler)
            thread=Thread(target=server.serve_forever,daemon=True);thread.start()
            origin=f"http://127.0.0.1:{server.server_port}/api/v1/"
            def request(path,bearer=owner_token,body=None):
                req=Request(origin+path,data=None if body is None else json.dumps(body).encode(),
                    headers={"Authorization":f"Bearer {bearer}","Content-Type":"application/json","Accept-Language":"en"})
                try:response=urlopen(req,timeout=30)
                except HTTPError as error:response=error
                with response:return response.status,json.load(response)
            try:
                url=f"shops/{shops[0].id}/team/{members[0].id}/restore-access/"
                status,preview=request(url)
                assert status==200 and preview["state"]=="pending"
                assert preview["membership"]["permissions"]==dict.fromkeys(flags,False)
                profile={**dict.fromkeys(flags,False),"can_collect_debt":True}
                body={"operation_id":str(uuid.uuid4()),"generation_id":preview["generation_id"],
                    "previous_review_version":0,"membership_version":1,"state":"confirmed",
                    "reason":"Seller and current permissions checked","permissions":profile}
                status,created=request(url,body=body)
                assert status==201 and created["membership"]["declared_permissions"]==profile
                assert created["membership"]["permissions"]["can_read_debt"] is True
                assert request(url,body=body)==(200,created)
                assert request("shops/",old_token)[0]==401
                fresh=token(seller)
                status,page=request("shops/",fresh)
                assert status==200 and page["count"]==1 and page["results"][0]["id"]==str(shops[0].id)
                assert request(f"shops/{shops[1].id}/",fresh)[0]==404
                assert request(url,fresh)[0]==403
                assert request(url,body={**body,"operation_id":str(uuid.uuid4())})[0]==409
                refusal={**body,"operation_id":str(uuid.uuid4()),"previous_review_version":1,
                    "membership_version":2,"state":"refused","reason":"Current access refused"}
                refusal.pop("permissions")
                assert request(url,body=refusal)[0]==201
                assert request(url,body=body)==(200,created)
                assert request("shops/",fresh)[1]["count"]==0
                status,current=request(url)
                assert status==200 and current["state"]=="refused" and current["membership"]["is_active"] is False
                history=request(f"shops/{shops[0].id}/team/{members[0].id}/history/")[1]
                assert history["count"]==2 and all(row["action"]=="restore_seller_access_decided" for row in history["results"])
                assert Activity.objects.count()==2 and RestoreAccessDecision.objects.filter(review__user=seller).count()==2
                seller.refresh_from_db();old_device.refresh_from_db()
                assert seller.is_active and not seller.is_staff and not seller.is_superuser and old_device.revoked_at is not None
                print(json.dumps({"restore_seller_http":"passed","shops":2,"fresh_explicit_profile":True,
                    "scope_a_only":True,"necessary_debt_reading":True,"archived_session_still_revoked":True,
                    "seller_cannot_revalidate":True,"replay_without_second_effect":True,"stale_version_409":True,
                    "refusal_blocks_existing_session_scope":True,"historical_replay_does_not_reactivate":True,
                    "paginated_history":True,"otp_exercised":False,"recovery_activation_exercised":False}))
            finally:
                server.shutdown();thread.join(timeout=5);server.server_close();connections.close_all()


if __name__=="__main__":
    main()
