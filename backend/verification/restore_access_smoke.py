"""Exercise scoped restoration decisions over HTTP on a disposable database.

This fixture supplies a new authenticated session; it does not qualify OTP,
the recovery UI, or activation of a restored production database.
"""
from datetime import timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid
from wsgiref.simple_server import make_server, WSGIRequestHandler


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


def main():
    with TemporaryDirectory(prefix="restore-access-http-") as directory:
        os.environ["DJANGO_SETTINGS_MODULE"] = "config.test_settings"
        os.environ["APP_DB_PATH"] = str(Path(directory)/"access.sqlite3")
        import django
        django.setup()
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.db import connections
        from django.utils import timezone
        from rest_framework_simplejwt.tokens import AccessToken
        from backups.models import RestoreAccessDecision, RestoreAccessGeneration, RestoreAccessReview,RestoreSession,RecoveryDevice
        from backups.services.access_decisions import decide_responsible_access
        from identity.models import DeviceSession, Installation, Membership, Shop, User
        from backups.models import RecoveryJournalDeposit,RecoveryJournalOperation
        from common.models import Activity
        from cash.models import CashMovement
        from stock.models import StockMovement
        from sales.models import Sale
        from customers.models import Customer
        from purchasing.models import Supplier

        call_command("migrate", verbosity=0)
        technical = User.objects.create_superuser(username="current-technical", password=None)
        owner = User.objects.create_user(username="restored-owner", is_active=False)
        shops = [Shop.objects.create(owner=owner, name=name) for name in ("A", "B")]
        source=User.objects.create_user(username="withdrawn-source",is_active=False)
        source_phone=Installation.objects.create(user=source,revoked_at=timezone.now())
        withdrawn=Membership.objects.create(user=source,shop=shops[0],is_active=False)
        customers=[Customer.objects.create(shop=shop,name="Client "+shop.name) for shop in shops]
        suppliers=[Supplier.objects.create(shop=shop,name="Fournisseur "+shop.name) for shop in shops]
        def reports(index):
            return (f"clients/{customers[index].id}/statement/",f"suppliers/{suppliers[index].id}/statement/","cash/")
        generation = RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(), backup_id=uuid.uuid4(), initiated_by=uuid.uuid4())
        flags = dict.fromkeys(("can_grant_credit", "can_collect_debt", "can_read_debt",
            "can_prepare_reminder", "can_read_cost", "can_read_cash_balance"), True)
        reviews = [RestoreAccessReview.objects.create(generation=generation, shop=shop, user=owner,
            archived_role="owner", was_active=True, archived_permissions=flags) for shop in shops]

        def decide(review, **values):
            return decide_responsible_access(**{"actor":technical, "review_id":review.id,
                "operation_id":uuid.uuid4(), "previous_version":0, "state":"confirmed",
                "reason":"Contrôle manuel de la fixture privée", **values})

        operation = uuid.uuid4()
        first, created = decide(reviews[0], operation_id=operation)
        assert created
        replay, created = decide(reviews[0], operation_id=operation)
        assert not created and replay.id == first.id
        owner.refresh_from_db()
        installation = Installation.objects.create(user=owner)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now()+timedelta(days=1))
        token = AccessToken.for_user(owner)
        token["session_id"] = str(session.id)
        server = make_server("127.0.0.1", 0, get_wsgi_application(), handler_class=QuietHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}/api/v1/shops/"
        try:
            def request_json(path,data=None):
                request = Request(path if path.startswith(root) else root+path,
                    data=json.dumps(data).encode() if data is not None else None,
                    headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"})
                try:
                    with urlopen(request, timeout=15) as response:
                        return response.status, json.load(response)
                except HTTPError as error:
                    with error:
                        return error.code, json.load(error)

            def get(path):return request_json(path)

            status, page = get("?page_size=1")
            assert status == 200 and page["count"] == 1 and page["results"][0]["id"] == str(shops[0].id)
            assert get(f"{shops[0].id}/")[0] == 200
            assert get(f"{shops[1].id}/")[0] == 404
            legacy=get(f"{shops[0].id}/products/")[1]["restoration"]
            assert legacy["state"]=="recovering" and legacy["known_devices"] is None and legacy["snapshot_at"] is None
            for suffix in reports(0):
                status,report=get(f"{shops[0].id}/"+suffix)
                assert status==200 and report["provisional"] and report["restoration"]["known_devices"] is None
            for suffix in reports(1):
                assert get(f"{shops[1].id}/"+suffix)[0]==404
            registry=RestoreSession.objects.create(generation=generation,
                snapshot_at=timezone.now()-timedelta(days=2),inventory_recorded_at=timezone.now())
            private=source_phone.id
            RecoveryDevice.objects.create(session=registry,shop=shops[0],origin_installation_id=private,
                origin_user_id=source.id,label="Private phone identity")
            for _ in range(3):
                RecoveryDevice.objects.create(session=registry,shop=shops[1],origin_installation_id=uuid.uuid4(),
                    origin_user_id=owner.id,label="Other scope")
            for suffix in ("products/","stock-alerts/",*reports(0)):
                status,profile=get(f"{shops[0].id}/"+suffix)
                assert status==200 and profile["restoration"]["known_devices"]==1
                assert profile["restoration"]["state"]=="recovering" and profile["restoration"]["snapshot_at"] is not None
                assert str(private) not in json.dumps(profile) and "Private phone identity" not in json.dumps(profile)
            assert get(f"{shops[1].id}/products/")[0]==404
            status,observations=get(f"{shops[0].id}/recovery/devices/?page_size=1")
            assert status==200 and observations["count"]==1
            assert observations["results"][0]["origin_installation_id"]==str(private)
            assert observations["restoration"]["state"]=="recovering"
            assert get(f"{shops[1].id}/recovery/devices/")[0]==404
            original_operation=uuid.uuid4()
            journal={"operation_id":str(uuid.uuid4()),"session_id":str(registry.id),"shop_id":str(shops[0].id),
                "format_version":1,"reason":"Copie survivante de la fixture privée","operations":[
                    {"operation_id":str(original_operation),"origin_installation_id":str(source_phone.id),
                     "origin_user_id":str(source.id),"operation_type":"sale","occurred_at":"2026-10-22T09:00:00Z",
                     "origin_recorded_at":"2026-10-22T09:01:00Z","payload":{"amount_fcfa":5000,"quantity_milli":1000},"dependencies":[]},
                    {"operation_id":str(uuid.uuid4()),"origin_installation_id":str(source_phone.id),
                     "origin_user_id":str(source.id),"operation_type":"customer_collection","occurred_at":"2026-10-22T09:05:00Z",
                     "origin_recorded_at":"2026-10-22T09:05:00Z","payload":{"amount_fcfa":3000},"dependencies":[str(original_operation)]}]}
            before=[model.objects.count() for model in (Activity,CashMovement,StockMovement,Sale,User)]
            status,captured=request_json(f"{shops[0].id}/recovery/journals/",journal)
            assert status==201 and captured["operation_count"]==2 and captured["business_effects_created"]==0
            status,replayed=request_json(f"{shops[0].id}/recovery/journals/",journal)
            assert status==200 and replayed==captured
            status,copied=request_json(f"{shops[0].id}/recovery/journals/",{**journal,"operation_id":str(uuid.uuid4())})
            assert status==201 and copied["new_operation_count"]==0 and copied["operations"]==captured["operations"]
            conflicting=json.loads(json.dumps(journal))
            conflicting["operations"][0]["payload"]["amount_fcfa"]=9999
            assert request_json(f"{shops[0].id}/recovery/journals/",conflicting)[0]==409
            invalid_unicode={**journal,"\ud800":"invalid"}
            assert request_json(f"{shops[0].id}/recovery/journals/",invalid_unicode)[0]==400
            status,source_page=get(f"{shops[0].id}/recovery/operations/?page_size=1")
            assert status==200 and source_page["count"]==2 and len(source_page["results"])==1
            assert get(source_page["next"])[1]["count"]==2
            assert get(f"{shops[1].id}/recovery/operations/")[0]==404
            assert RecoveryJournalDeposit.objects.count()==2 and RecoveryJournalOperation.objects.count()==2
            assert [model.objects.count() for model in (Activity,CashMovement,StockMovement,Sale,User)]==before
            source.refresh_from_db()
            source_phone.refresh_from_db()
            withdrawn.refresh_from_db()
            assert not source.is_active and not withdrawn.is_active and source_phone.revoked_at is not None
            decide(reviews[1])
            assert get("")[1]["count"] == 2
            assert get(f"{shops[1].id}/recovery/devices/")[1]["count"]==3
            for suffix in reports(1):
                status,report=get(f"{shops[1].id}/"+suffix)
                assert status==200 and report["restoration"]["known_devices"]==3 and report["provisional"]
            decide(reviews[0], previous_version=1, state="refused", reason="Nouveau contrôle non concluant")
            assert get(f"{shops[0].id}/")[0] == 404 and get(f"{shops[1].id}/")[0] == 200
            assert get(f"{shops[0].id}/recovery/devices/")[0]==404
            assert request_json(f"{shops[0].id}/recovery/journals/",journal)[0]==404
            assert get(f"{shops[0].id}/recovery/operations/")[0]==404
            assert get(f"{shops[1].id}/recovery/operations/")[1]["count"]==0
            for suffix in reports(0):
                assert get(f"{shops[0].id}/"+suffix)[0]==404
            for suffix in reports(1):
                assert get(f"{shops[1].id}/"+suffix)[0]==200
            assert RestoreAccessDecision.objects.get(id=first.id).state == "confirmed"
            assert RestoreAccessDecision.objects.count() == 3
            print(json.dumps({"restore_access_http":"passed", "shops":2,
                "a_confirmed_b_pending_filtered_before_pagination":True,
                "b_confirmed_separately":True, "a_refused_b_still_accessible":True,
                "same_session_rechecks_current_decisions":True, "decision_history_preserved":True,
                "idempotent_confirmation":True,"legacy_recovery_inventory_unknown":True,
                "catalogue_and_alert_recovery_profile_scoped":True,"device_identity_not_exposed":True,
                "responsible_device_list_scoped":True,"refusal_blocks_device_inspection":True,
                "financial_report_recovery_metadata_scoped":True,"refusal_blocks_financial_reports":True,
                "durable_journal_capture_and_replay":True,"another_copy_reuses_captures":True,
                "changed_source_uuid_conflicts":True,"withdrawn_origin_not_reactivated":True,
                "capture_creates_no_business_effects":True,"refusal_blocks_journal_deposit":True,
                "invalid_unicode_rejected_without_server_error":True,
                "recovery_not_declared_complete":True, "otp_exercised":False, "production_activation":False}))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            connections.close_all()


if __name__ == "__main__":
    main()
