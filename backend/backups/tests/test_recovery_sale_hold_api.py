"""Review pending source facts without charging stock or cash a second time."""
import uuid
import json

from django.db import connection,IntegrityError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from backups import models as backups
from backups.tests import test_recovery_sale_integration as fixtures
from cash.models import CashMovement
from common.models import Activity
from customers.models import Customer,CustomerDebt
from identity.models import User,Membership,DeviceSession
from sales.models import Sale
from stock.models import StockReservation,StockAlertEvent
from stock.services.stock_queries import with_stock


class RecoverySaleHoldApiTests(TestCase):
    setUp=fixtures.RecoverySaleIntegrationTests.setUp
    authenticate=fixtures.RecoverySaleIntegrationTests.authenticate
    generation=fixtures.RecoverySaleIntegrationTests.generation
    record=fixtures.RecoverySaleIntegrationTests.record
    device=fixtures.RecoverySaleIntegrationTests.device
    review=fixtures.RecoverySaleIntegrationTests.review
    payload=fixtures.RecoverySaleIntegrationTests.payload
    url=fixtures.RecoverySaleIntegrationTests.url
    source=fixtures.RecoverySaleIntegrationTests.source
    capture=fixtures.RecoverySaleIntegrationTests.capture
    preview=fixtures.RecoverySaleIntegrationTests.preview
    apply=fixtures.RecoverySaleIntegrationTests.apply

    def stock(self):
        return with_stock(type(self.product).objects.filter(id=self.product.id)).get()

    def decide(self,capture,*,state="pending",preview=None,**changes):
        preview=preview or self.preview(capture)
        command={"operation_id":str(uuid.uuid4()),"previous_version":preview.get("review_version",0),
            "preview_token":preview["preview_token"],"state":state,"reason":"Contrôle de la copie physique",**changes}
        return self.client.post(self.url(f"operations/{capture.id}/review/"),command,format="json"),command

    def stage(self,capture):
        response,command=self.decide(capture)
        self.assertEqual(response.status_code,201,response.content)
        return response,command

    def test_pending_sale_reserves_once_then_validation_replaces_the_reservation_without_second_alert(self):
        capture=self.capture(self.source())
        pending,command=self.stage(capture)
        repeated=self.client.post(self.url(f"operations/{capture.id}/review/"),command,format="json")
        self.assertEqual((repeated.status_code,repeated.data),(200,pending.data))
        self.assertEqual(StockReservation.objects.count(),1)
        stock=self.stock()
        self.assertEqual((stock.stock_validated_milli,stock.stock_reserved_milli,stock.stock_available_milli),("3000","1000","2000"))
        self.assertEqual((Sale.objects.count(),CashMovement.objects.count()),(0,0))
        self.assertEqual(StockAlertEvent.objects.count(),1)
        page=self.client.get(self.url("sales-review/"),{"state":"pending"})
        self.assertEqual(page.status_code,200,page.content)
        self.assertEqual(page.data["count"],1)
        self.assertEqual(page.data["totals"]["pending"],{"net_total_fcfa":"500","payments_fcfa":"500","unpaid_debt_fcfa":"0","customer_credit_fcfa":"0"})
        preview=self.preview(capture)
        self.assertEqual((preview["review_state"],preview["review_version"]),("pending",1))
        self.assertEqual((preview["stocks"][0]["validated_after_milli"],preview["stocks"][0]["reserved_after_milli"],
            preview["stocks"][0]["available_after_milli"]),("2000","0","2000"))
        applied,_=self.apply(capture,preview)
        self.assertEqual(applied.status_code,201,applied.content)
        self.assertEqual(backups.RecoverySaleHold.objects.get().state,"validated")
        stock=self.stock()
        self.assertEqual((stock.stock_validated_milli,stock.stock_reserved_milli,stock.stock_available_milli),("2000","0","2000"))
        self.assertEqual((CashMovement.objects.count(),CashMovement.objects.get().amount_fcfa,StockAlertEvent.objects.count()),(1,500,1))
        history=self.client.get(self.url(f"operations/{capture.id}/review-decisions/"))
        self.assertEqual(history.status_code,200,history.content)
        self.assertEqual([row["state"] for row in history.data["results"]],["pending","validated"])
        self.assertEqual(self.client.post(self.url(f"operations/{capture.id}/review/"),command,format="json").data,pending.data)

    def test_refusal_keeps_money_separate_and_stock_reserved_until_explicit_regularization(self):
        source=self.source()
        customer=Customer.objects.create(shop=self.a,name="Client de la copie")
        source.payload["original_request"].update(mode="credit",customer_id=str(customer.id))
        source.payload["sale"].update(mode="credit",customer_id=str(customer.id))
        for payment in (source.payload["original_request"]["payments"][0],source.payload["sale"]["payments"][0]):
            payment.update(amount_fcfa=200,received_fcfa=200)
        capture=self.capture(source)
        self.stage(capture)
        refused,command=self.decide(capture,state="refused",reason="Quantité à rapprocher, argent réellement reçu conservé")
        self.assertEqual(refused.status_code,201,refused.content)
        self.assertEqual(StockReservation.objects.get().state,"rejected")
        self.assertEqual(self.stock().stock_available_milli,"2000")
        self.assertEqual((Sale.objects.count(),CashMovement.objects.count(),CustomerDebt.objects.count()),(0,0,0))
        preview=self.preview(capture)
        self.assertFalse(preview["can_apply"])
        self.assertIn("recovery_sale_refused",{issue["code"] for issue in preview["issues"]})
        applied,_=self.apply(capture,preview)
        self.assertEqual(applied.status_code,409,applied.content)
        self.assertEqual(self.decide(capture,state="pending",preview=preview)[0].status_code,409)
        page=self.client.get(self.url("sales-review/"),{"state":"refused"})
        self.assertEqual(page.data["totals"]["refused"],{"net_total_fcfa":"500","payments_fcfa":"200","unpaid_debt_fcfa":"300","customer_credit_fcfa":"0"})
        self.assertEqual(self.client.post(self.url(f"operations/{capture.id}/review/"),command,format="json").status_code,200)
        self.assertTrue(backups.RecoveryJournalOperation.objects.filter(id=capture.id).exists())

    def test_another_generation_and_repeated_product_lines_share_the_same_hold(self):
        source=self.source()
        second=str(uuid.uuid4())
        source.payload["sale"]["lines"].append({**source.payload["sale"]["lines"][0],"id":second})
        source.payload["original_request"]["lines"].append({**source.payload["original_request"]["lines"][0],"id":second})
        source.payload["sale"].update(reference_total_fcfa=1000,net_total_fcfa=1000)
        for payment in (source.payload["sale"]["payments"][0],source.payload["original_request"]["payments"][0]):
            payment.update(amount_fcfa=1000,received_fcfa=1000)
        first=self.capture(source)
        self.stage(first)
        self.assertEqual(StockReservation.objects.get().quantity_milli,2000)
        self.current.current_slot=None;self.current.save()
        self.current=self.generation();self.registry=self.record(self.current)
        self.review(self.a,self.owner,"owner",confirmed=True)
        next_capture=self.capture(source)
        self.stage(next_capture)
        self.assertEqual((backups.RecoverySaleHold.objects.count(),StockReservation.objects.count()),(1,1))
        self.assertEqual(self.stock().stock_available_milli,"1000")
        response,_=self.apply(next_capture)
        self.assertEqual(response.status_code,201,response.content)
        self.assertEqual((self.stock().stock_validated_milli,self.stock().stock_available_milli),("1000","1000"))

    def test_permission_scope_stale_preview_and_reservation_tampering_are_rejected(self):
        capture=self.capture(self.source())
        seller=User.objects.create_user(username="cannot-review-pending")
        member=Membership.objects.create(user=seller,shop=self.a)
        self.review(self.a,seller,"seller",membership=member,confirmed=True)
        seller_api=self.authenticate(seller)
        path=self.url(f"operations/{capture.id}/review/")
        self.assertEqual(seller_api.generic("POST",path,b"not-json",content_type="application/json").status_code,403)
        self.assertEqual(self.client.post(self.url(f"operations/{capture.id}/review/",shop=self.b),{},format="json").status_code,404)
        old=self.preview(capture)
        self.product.version+=1;self.product.save()
        self.assertEqual(self.decide(capture,preview=old)[0].status_code,409)
        self.stage(capture)
        StockReservation.objects.update(quantity_milli=2000)
        self.assertEqual(self.client.get(self.url(f"operations/{capture.id}/integration-preview/")).status_code,409)
        self.assertEqual(Sale.objects.count(),0)

    def test_database_failure_rolls_back_staging_and_an_approval_restores_its_pending_reservation(self):
        capture=self.capture(self.source())
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER fail_hold_receipt BEFORE UPDATE OF receipt ON backups_recoverysaleholddecision BEGIN SELECT RAISE(ABORT,'test hold receipt'); END")
        try:
            with self.assertRaises(IntegrityError):self.decide(capture)
        finally:
            with connection.cursor() as cursor:cursor.execute("DROP TRIGGER fail_hold_receipt")
        self.assertEqual((backups.RecoverySaleHold.objects.count(),StockReservation.objects.count(),StockAlertEvent.objects.count()),(0,0,0))
        self.stage(capture)
        before=self.preview(capture)
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER fail_hold_approval BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'test hold approval'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(capture,before)
        finally:
            with connection.cursor() as cursor:cursor.execute("DROP TRIGGER fail_hold_approval")
        self.assertEqual((StockReservation.objects.get().state,backups.RecoverySaleHold.objects.get().state),("pending","pending"))
        self.assertEqual((Sale.objects.count(),CashMovement.objects.count(),StockAlertEvent.objects.count()),(0,0,1))
        self.assertEqual(self.preview(capture)["preview_token"],before["preview_token"])
        self.assertEqual(self.apply(capture,before)[0].status_code,201)

    def test_review_totals_cover_the_full_sql_filter_and_do_not_include_another_shop(self):
        for _ in range(3):self.stage(self.capture(self.source()))
        response=self.client.get(self.url("sales-review/"),{"page_size":1,"state":"pending"})
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual((response.data["count"],len(response.data["results"])),(3,1))
        self.assertEqual(response.data["totals"]["pending"]["payments_fcfa"],"1500")
        b_review=backups.RestoreAccessReview.objects.get(generation=self.current,shop=self.b,user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=b_review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=0,version=1,state="confirmed",reason="Contrôle B")
        other=self.client.get(self.url("sales-review/",shop=self.b))
        self.assertEqual(other.status_code,200,other.content)
        self.assertEqual((other.data["count"],other.data["totals"]["pending"]["payments_fcfa"]),(0,"0"))

    def test_large_pending_money_totals_are_exact_and_pages_do_not_load_source_payloads_per_row(self):
        customer=Customer.objects.create(shop=self.a,name="Monnaie due déclarée")
        for _ in range(2):
            source=self.source()
            source.payload["original_request"]["customer_id"]=str(customer.id)
            source.payload["sale"]["customer_id"]=str(customer.id)
            for payment in (source.payload["sale"]["payments"][0],source.payload["original_request"]["payments"][0]):
                payment.update(amount_fcfa=9223372036854775807,received_fcfa=9223372036854775807)
            self.stage(self.capture(source))
        with CaptureQueriesContext(connection) as small:
            first=self.client.get(self.url("sales-review/"),{"page_size":1})
        with CaptureQueriesContext(connection) as larger:
            second=self.client.get(self.url("sales-review/"),{"page_size":2})
        self.assertEqual((first.status_code,second.status_code),(200,200))
        self.assertEqual(first.data["totals"]["pending"],{"net_total_fcfa":"1000","payments_fcfa":"18446744073709551614",
            "unpaid_debt_fcfa":"0","customer_credit_fcfa":"18446744073709550614"})
        self.assertEqual(len(small),len(larger))
        self.assertTrue(any('FROM "backups_recoverysalehold"' in row["sql"] and "LIMIT 1" in row["sql"] for row in small))
        self.assertFalse(any('"backups_recoveryjournaloperation"."payload"' in row["sql"] for row in larger))
        self.assertEqual((CashMovement.objects.count(),CustomerDebt.objects.count()),(0,0))

    def test_receipts_history_pages_and_errors_match_the_actual_openapi_contract(self):
        from drf_spectacular.generators import SchemaGenerator
        from jsonschema import Draft4Validator,FormatChecker
        from rest_framework.test import APIClient
        def nullable(value):
            if isinstance(value,list):return [nullable(item) for item in value]
            if not isinstance(value,dict):return value
            result={key:nullable(item) for key,item in value.items() if key!="nullable"}
            return {"anyOf":[result,{"type":"null"}]} if value.get("nullable") else result
        schema=SchemaGenerator().get_schema(request=None,public=True)
        def check(response,ending,method="get"):
            path=next(path for path in schema["paths"] if path.endswith(ending))
            document=schema["paths"][path][method]["responses"][str(response.status_code)]
            rule=nullable({**document["content"]["application/json"]["schema"],"components":schema["components"]})
            errors=list(Draft4Validator(rule,format_checker=FormatChecker()).iter_errors(json.loads(response.content)))
            self.assertEqual([(list(error.absolute_path),error.validator) for error in errors],[])
        capture=self.capture(self.source())
        history_path=self.url(f"operations/{capture.id}/review-decisions/")
        check(self.client.get(history_path),"/review-decisions/")
        response,command=self.stage(capture)
        check(response,"/review/","post")
        review_path=self.url(f"operations/{capture.id}/review/")
        check(self.client.post(review_path,command,format="json"),"/review/","post")
        check(self.client.post(review_path,{**command,"reason":"Changed"},format="json"),"/review/","post")
        check(self.client.post(review_path,{},format="json"),"/review/","post")
        check(self.client.get(history_path),"/review-decisions/")
        check(self.client.get(self.url("sales-review/")),"/sales-review/")
        legacy=dict(response.data)
        legacy.pop("source_sale_id")
        backups.RecoverySaleHoldDecision.objects.filter(id=response.data["id"]).update(receipt=legacy)
        old_receipt=self.client.post(review_path,command,format="json")
        self.assertEqual(old_receipt.data,legacy)
        check(old_receipt,"/review/","post")
        check(self.client.get(history_path),"/review-decisions/")
        check(APIClient().get(self.url("sales-review/")),"/sales-review/")
        check(self.client.get(self.url(f"operations/{uuid.uuid4()}/review-decisions/")),"/review-decisions/")

    def test_revoked_current_responsible_cannot_replay_a_hold_command_or_read_its_history(self):
        capture=self.capture(self.source())
        _response,command=self.stage(capture)
        review=backups.RestoreAccessReview.objects.get(generation=self.current,shop=self.a,user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=1,version=2,state="refused",reason="Droit actuel retiré")
        self.assertEqual(self.client.post(self.url(f"operations/{capture.id}/review/"),command,format="json").status_code,404)
        self.assertEqual(self.client.get(self.url(f"operations/{capture.id}/review-decisions/")).status_code,404)
        self.assertEqual(self.client.get(self.url("sales-review/")).status_code,404)
        self.assertEqual((backups.RecoverySaleHold.objects.count(),StockReservation.objects.count()),(1,1))

    def test_review_denial_is_translated_without_reading_or_storing_the_submitted_body(self):
        capture=self.capture(self.source())
        seller=User.objects.create_user(username="translated-recovery-review")
        member=Membership.objects.create(user=seller,shop=self.a)
        self.review(self.a,seller,"seller",membership=member,confirmed=True)
        client=self.authenticate(seller)
        for language,message in (("fr","Seul un responsable actuel de la boutique peut examiner les ventes récupérées."),
                                 ("en","Only a current shop responsible can review recovered sales.")):
            with self.subTest(language=language):
                response=client.generic("POST",self.url(f"operations/{capture.id}/review/"),b"not-json",
                    content_type="application/json",HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(response.status_code,403,response.content)
                self.assertEqual(str(response.data["detail"]),message)
        self.assertEqual(backups.RecoverySaleHold.objects.count(),0)

    def test_ordinary_sale_endpoint_cannot_bypass_pending_responsible_review(self):
        source=self.source()
        source.origin_user_id=self.owner.id
        source.origin_installation_id=DeviceSession.objects.get(installation__user=self.owner).installation_id
        source.payload["original_request"].update(occurred_at=source.occurred_at.isoformat(),reason="Vente réelle retrouvée")
        source.payload["original_request"]["lines"][0]["product_version"]=8
        source.payload["sale"]["lines"][0].update(product_version=8,product_name=self.product.name,
            unit_price_fcfa=900,reference_fcfa=900,net_fcfa=900)
        source.payload["sale"].update(reference_total_fcfa=900,net_total_fcfa=900)
        for payment in (source.payload["original_request"]["payments"][0],source.payload["sale"]["payments"][0]):
            payment.update(amount_fcfa=900,received_fcfa=900)
        capture=self.capture(source)
        self.stage(capture)
        native=self.client.post(f"/api/v1/shops/{self.a.id}/sales/",source.payload["original_request"],format="json")
        self.assertEqual(native.status_code,409,native.content)
        self.assertEqual((Sale.objects.count(),CashMovement.objects.count()),(0,0))
        self.assertEqual(self.stock().stock_available_milli,"2000")
        preview=self.preview(capture)
        self.assertEqual(preview["outcome"],"new")
        self.assertEqual(preview["stocks"][0]["available_after_milli"],"2000")
        response,_=self.apply(capture,preview)
        self.assertEqual(response.status_code,201,response.content)
        self.assertEqual((self.stock().stock_validated_milli,self.stock().stock_available_milli),("2000","2000"))
        self.assertEqual((Sale.objects.count(),CashMovement.objects.count(),CashMovement.objects.get().amount_fcfa),(1,1,900))
        self.assertEqual(backups.RecoverySaleHold.objects.get().state,"validated")
        self.assertEqual(StockAlertEvent.objects.count(),1)
        repeated=self.client.post(f"/api/v1/shops/{self.a.id}/sales/",source.payload["original_request"],format="json")
        self.assertEqual(repeated.status_code,200,repeated.content)
        self.assertEqual(CashMovement.objects.count(),1)

    def test_changing_only_the_operation_uuid_cannot_charge_or_reserve_the_same_sale_twice(self):
        source=self.source()
        capture=self.capture(source)
        self.stage(capture)
        source.operation_id=uuid.uuid4()
        source.payload["original_request"]["operation_id"]=str(source.operation_id)
        copied=self.capture(source)
        preview=self.preview(copied)
        self.assertFalse(preview["can_hold"])
        self.assertIn("entity_uuid_conflict",{row["code"] for row in preview["issues"]})
        self.assertEqual(self.decide(copied,preview=preview)[0].status_code,409)
        request=source.payload["original_request"]
        request["lines"][0]["product_version"]=8
        request["payments"][0].update(amount_fcfa=900,received_fcfa=900)
        native=self.client.post(f"/api/v1/shops/{self.a.id}/sales/",request,format="json")
        self.assertEqual(native.status_code,409,native.content)
        self.assertEqual((StockReservation.objects.count(),Sale.objects.count(),CashMovement.objects.count()),(1,0,0))
