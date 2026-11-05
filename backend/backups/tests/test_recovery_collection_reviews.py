"""Pending/refused collection money never masquerades as validated receipts."""
from copy import deepcopy
import uuid

from django.db import connection,IntegrityError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from backups import models as backups
from backups.tests import test_recovery_collections as fixtures
from cash.models import CashMovement
from common.models import Activity
from customers.models import CustomerDebt,CustomerPayment,CustomerLedgerEntry
from stock.models import StockReservation,StockMovement
from identity.models import User,Membership


class RecoveryCollectionReviewTests(TestCase):
    setUp=fixtures.RecoveryCollectionTests.setUp
    authenticate=fixtures.RecoveryCollectionTests.authenticate
    generation=fixtures.RecoveryCollectionTests.generation
    record=fixtures.RecoveryCollectionTests.record
    device=fixtures.RecoveryCollectionTests.device
    review=fixtures.RecoveryCollectionTests.review
    payload=fixtures.RecoveryCollectionTests.payload
    url=fixtures.RecoveryCollectionTests.url
    source=fixtures.RecoveryCollectionTests.source
    capture=fixtures.RecoveryCollectionTests.capture
    preview=fixtures.RecoveryCollectionTests.preview
    apply=fixtures.RecoveryCollectionTests.apply

    def decide(self,capture,state="pending",preview=None,**changes):
        preview=preview or self.preview(capture)
        body={"operation_id":str(uuid.uuid4()),"previous_version":preview["review_version"],
            "preview_token":preview["preview_token"],"state":state,"reason":"Montants et origine examinés",**changes}
        return self.client.post(self.url(f"operations/{capture.id}/collection-review/"),body,format="json"),body

    def stage(self,capture):
        response,command=self.decide(capture)
        self.assertEqual(response.status_code,201,response.content)
        return response,command

    def test_pending_cash_is_separate_and_approval_commits_one_effect_with_linked_history(self):
        capture=self.capture(self.source());preview=self.preview(capture)
        self.assertTrue(preview["can_mark_pending"])
        staged,command=self.stage(capture)
        self.assertEqual(staged.data["amount_fcfa"],5000)
        self.assertEqual((CustomerPayment.objects.count(),CashMovement.objects.count(),CustomerLedgerEntry.objects.count(),
            StockReservation.objects.count(),StockMovement.objects.count()),(0,0,0,0,0))
        self.assertEqual(sum(CustomerDebt.objects.values_list("remaining_fcfa",flat=True)),10000)
        repeated=self.client.post(self.url(f"operations/{capture.id}/collection-review/"),command,format="json")
        self.assertEqual((repeated.status_code,repeated.data),(200,staged.data))
        page=self.client.get(self.url("collections-review/"),{"page_size":1,"state":"pending"})
        self.assertEqual(page.status_code,200,page.content)
        self.assertEqual((page.data["count"],page.data["totals"]["pending"]["amount_fcfa"]),(1,"5000"))
        preview=self.preview(capture)
        self.assertEqual((preview["review_state"],preview["review_version"]),("pending",1))
        response,_=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)
        review=backups.RecoveryCollectionReview.objects.get()
        self.assertEqual((review.state,review.version),("validated",2))
        self.assertEqual((CustomerPayment.objects.count(),CashMovement.objects.count(),CustomerLedgerEntry.objects.count()),(1,2,1))
        self.assertEqual(review.decisions.get(state="validated").integration_decision_id,uuid.UUID(response.data["id"]))
        history=self.client.get(self.url(f"operations/{capture.id}/collection-review-decisions/"),{"page_size":1})
        self.assertEqual((history.status_code,history.data["count"]),(200,2))
        self.assertEqual(history.data["results"][0],staged.data)
        validated=self.client.get(self.url("collections-review/"),{"state":"validated"})
        self.assertEqual(validated.data["totals"]["validated"]["amount_fcfa"],"5000")
        self.assertEqual(validated.data["totals"]["pending"]["amount_fcfa"],"0")

    def test_refusal_keeps_source_money_history_without_ledger_refund_or_reopening(self):
        capture=self.capture(self.source());staged,_=self.stage(capture)
        refused,command=self.decide(capture,state="refused")
        self.assertEqual(refused.status_code,201,refused.content)
        preview=self.preview(capture)
        self.assertFalse(preview["can_apply"])
        self.assertIn("recovery_collection_refused",{row["code"] for row in preview["issues"]})
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        self.assertEqual(self.decide(capture,preview=preview)[0].status_code,409)
        self.assertEqual((CustomerPayment.objects.count(),CashMovement.objects.count(),CustomerLedgerEntry.objects.count()),(0,0,0))
        self.assertEqual(sum(CustomerDebt.objects.values_list("remaining_fcfa",flat=True)),10000)
        self.assertTrue(backups.RecoveryJournalOperation.objects.filter(id=capture.id).exists())
        page=self.client.get(self.url("collections-review/"),{"state":"refused"})
        self.assertEqual(page.data["totals"]["refused"],{"amount_fcfa":"5000","original_applied_fcfa":"5000","original_unallocated_fcfa":"0"})
        history=self.client.get(self.url(f"operations/{capture.id}/collection-review-decisions/"))
        self.assertEqual(history.data["results"],[staged.data,refused.data])
        retry=self.client.post(self.url(f"operations/{capture.id}/collection-review/"),command,format="json")
        self.assertEqual((retry.status_code,retry.data),(200,refused.data))

    def test_generations_share_one_review_and_source_totals_remain_historical(self):
        source=self.source();first=self.capture(source);self.stage(first)
        self.current.current_slot=None;self.current.save();self.current=self.generation();self.registry=self.record(self.current)
        self.review(self.a,self.owner,"owner",confirmed=True)
        page=self.client.get(self.url("collections-review/"))
        self.assertIsNone(page.data["results"][0]["current_capture_id"])
        self.assertEqual(self.client.get(self.url(f"operations/{first.id}/collection-review-decisions/")).status_code,404)
        second=self.capture(source);self.stage(second)
        self.assertEqual(backups.RecoveryCollectionReview.objects.count(),1)
        CustomerDebt.objects.filter(customer=self.customer).update(remaining_fcfa=0)
        response,_=self.apply(second);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(CustomerPayment.objects.get().unallocated_fcfa,5000)
        page=self.client.get(self.url("collections-review/"))
        self.assertEqual(page.data["results"][0]["current_capture_id"],str(second.id))
        self.assertEqual(page.data["totals"]["validated"]["original_applied_fcfa"],"5000")
        self.assertEqual(backups.RecoveryCollectionReviewDecision.objects.count(),3)

    def test_native_entity_uuid_bypass_and_another_source_with_same_payment_id_are_blocked(self):
        source=self.source();capture=self.capture(source);self.stage(capture)
        native={key:value for key,value in source.payload["original_request"].items() if key not in ("customer_id","installation_id")}
        native["operation_id"]=str(uuid.uuid4())
        response=self.client.post(f"/api/v1/shops/{self.a.id}/clients/{self.customer.id}/payments/",native,format="json")
        self.assertEqual(response.status_code,409,response.content)
        second=deepcopy(source);second.operation_id=uuid.uuid4();second.payload["original_request"]["operation_id"]=str(second.operation_id)
        other=self.capture(second);preview=self.preview(other)
        self.assertFalse(preview["can_apply"])
        self.assertIn("entity_uuid_conflict",{row["code"] for row in preview["issues"]})
        self.assertEqual(CustomerPayment.objects.count(),0)

    def test_late_database_failure_restores_pending_state_and_all_balances(self):
        capture=self.capture(self.source());self.stage(capture);preview=self.preview(capture)
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER fail_collection_review_validation BEFORE INSERT ON backups_recoverycollectionreviewdecision WHEN NEW.state='validated' BEGIN SELECT RAISE(ABORT,'injected validation failure'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(capture,preview)
        finally:
            with connection.cursor() as cursor:cursor.execute("DROP TRIGGER fail_collection_review_validation")
        self.assertEqual((CustomerPayment.objects.count(),CashMovement.objects.count(),CustomerLedgerEntry.objects.count(),
            backups.RecoveredOperationOrigin.objects.count(),backups.RecoveryIntegrationDecision.objects.count()),(0,0,0,0,0))
        self.assertEqual((backups.RecoveryCollectionReview.objects.get().state,backups.RecoveryCollectionReview.objects.get().version),("pending",1))
        self.assertEqual(sum(CustomerDebt.objects.values_list("remaining_fcfa",flat=True)),10000)
        self.assertEqual(self.preview(capture)["preview_token"],preview["preview_token"])

    def test_seller_foreign_shop_stale_preview_and_sale_review_route_do_not_apply(self):
        capture=self.capture(self.source());preview=self.preview(capture)
        self.customer.version+=1;self.customer.save()
        self.assertEqual(self.decide(capture,preview=preview)[0].status_code,409)
        seller=User.objects.create_user(username="cannot-review-collection")
        member=Membership.objects.create(user=seller,shop=self.a)
        self.review(self.a,seller,"seller",membership=member,confirmed=True)
        seller_api=self.authenticate(seller)
        self.assertEqual(seller_api.get(self.url("collections-review/")).status_code,403)
        self.assertEqual(seller_api.generic("POST",self.url(f"operations/{capture.id}/collection-review/"),b"invalid",content_type="application/json").status_code,403)
        self.assertEqual(self.client.post(self.url(f"operations/{capture.id}/collection-review/",shop=self.b),{},format="json").status_code,404)
        self.stage(capture)
        fresh=self.preview(capture)
        command={"operation_id":str(uuid.uuid4()),"previous_version":fresh["review_version"],"preview_token":fresh["preview_token"],"state":"refused","reason":"Mauvaise famille"}
        response=self.client.post(self.url(f"operations/{capture.id}/review/"),command,format="json")
        self.assertEqual(response.status_code,400,response.content)
        self.assertEqual(backups.RecoveryCollectionReview.objects.get().state,"pending")

    def test_queue_totals_cover_the_filter_beyond_int64_and_payloads_are_not_loaded(self):
        from sales.services.pricing import MAX_FCFA
        for _ in range(2):
            source=self.source();snapshot=source.payload["collection"];raw=source.payload["original_request"]
            raw["parts"]=raw["parts"][:1];raw["parts"][0]["amount_fcfa"]=MAX_FCFA
            snapshot["parts"]=snapshot["parts"][:1];snapshot["parts"][0]["amount_fcfa"]=MAX_FCFA
            snapshot.update(amount_fcfa=MAX_FCFA,unallocated_fcfa=MAX_FCFA-5000,requires_regularization=True)
            self.stage(self.capture(source))
        with CaptureQueriesContext(connection) as queries:
            page=self.client.get(self.url("collections-review/"),{"state":"pending","page_size":1,"customer_id":str(self.customer.id)})
        self.assertEqual(page.status_code,200,page.content)
        self.assertEqual((page.data["count"],len(page.data["results"]),page.data["totals"]["pending"]["amount_fcfa"]),(2,1,str(2*MAX_FCFA)))
        self.assertEqual(page.data["totals"]["pending"]["original_unallocated_fcfa"],str(2*MAX_FCFA-10000))
        self.assertTrue(any('LIMIT 1' in row["sql"] for row in queries.captured_queries))
        self.assertFalse(any('"payload"' in row["sql"] or '"dependencies"' in row["sql"] for row in queries.captured_queries))
        empty=self.client.get(self.url("collections-review/"),{"customer_id":str(uuid.uuid4())})
        self.assertEqual((empty.data["count"],empty.data["totals"]["pending"]["amount_fcfa"]),(0,"0"))
        self.assertEqual(CustomerPayment.objects.count(),0)

    def test_failed_staging_and_tampered_review_leave_source_and_ledgers_untouched(self):
        capture=self.capture(self.source());preview=self.preview(capture)
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER fail_collection_staging BEFORE UPDATE OF receipt ON backups_recoverycollectionreviewdecision BEGIN SELECT RAISE(ABORT,'injected staging failure'); END")
        try:
            with self.assertRaises(IntegrityError):self.decide(capture,preview=preview)
        finally:
            with connection.cursor() as cursor:cursor.execute("DROP TRIGGER fail_collection_staging")
        self.assertEqual((backups.RecoveryCollectionReview.objects.count(),backups.RecoveryCollectionReviewDecision.objects.count()),(0,0))
        self.assertFalse(Activity.objects.filter(action="recovery_collection_pending").exists())
        self.assertEqual(self.preview(capture)["preview_token"],preview["preview_token"])
        self.stage(capture)
        backups.RecoveryCollectionReview.objects.update(state="refused")
        self.assertEqual(self.client.get(self.url(f"operations/{capture.id}/integration-preview/")).status_code,409)
        self.assertEqual((CustomerPayment.objects.count(),CashMovement.objects.count()),(0,0))

    def test_revoked_rights_and_uuid_collisions_are_rejected_even_on_exact_review_retry(self):
        capture=self.capture(self.source());preview=self.preview(capture)
        for operation_id in (capture.operation_id,self.debts[0].operation_id):
            with self.subTest(operation_id=operation_id):
                self.assertEqual(self.decide(capture,preview=preview,operation_id=str(operation_id))[0].status_code,409)
        response,command=self.stage(capture)
        self.assertEqual(self.decide(capture,preview=preview)[0].status_code,409)
        review=backups.RestoreAccessReview.objects.get(generation=self.current,shop=self.a,user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=1,version=2,state="refused",reason="Droits actuels retirés")
        retry=self.client.post(self.url(f"operations/{capture.id}/collection-review/"),command,format="json")
        self.assertEqual(retry.status_code,404,retry.content)
        self.assertEqual(backups.RecoveryCollectionReviewDecision.objects.count(),1)

    def test_review_queue_history_receipts_and_errors_match_openapi(self):
        from drf_spectacular.generators import SchemaGenerator
        from jsonschema import Draft4Validator,FormatChecker
        import json
        def nullable(value):
            if isinstance(value,list):return [nullable(item) for item in value]
            if not isinstance(value,dict):return value
            result={key:nullable(item) for key,item in value.items() if key!="nullable"}
            return {"anyOf":[result,{"type":"null"}]} if value.get("nullable") else result
        schema=SchemaGenerator().get_schema(request=None,public=True)
        def check(response,ending,method="get"):
            path=next(path for path in schema["paths"] if path.endswith(ending))
            rule=schema["paths"][path][method]["responses"][str(response.status_code)]["content"]["application/json"]["schema"]
            errors=list(Draft4Validator(nullable({**rule,"components":schema["components"]}),format_checker=FormatChecker()).iter_errors(json.loads(response.content)))
            self.assertEqual([(list(error.absolute_path),error.validator) for error in errors],[])
        capture=self.capture(self.source());response,command=self.stage(capture);check(response,"/collection-review/","post")
        check(self.client.get(self.url("collections-review/")),"/collections-review/")
        check(self.client.get(self.url(f"operations/{capture.id}/collection-review-decisions/")),"/collection-review-decisions/")
        check(self.client.post(self.url(f"operations/{capture.id}/collection-review/"),command,format="json"),"/collection-review/","post")
        check(self.client.post(self.url(f"operations/{capture.id}/collection-review/"),{**command,"reason":"Changed"},format="json"),"/collection-review/","post")
        response,_=self.decide(capture,state="refused");check(response,"/collection-review/","post")

    def test_technical_unfold_lists_are_bilingual_read_only_and_deny_business_staff(self):
        capture=self.capture(self.source());self.stage(capture)
        self.client.force_login(self.technical)
        for language,label in (("fr","Encaissements récupérés à examiner"),("en","Recovered collections awaiting review")):
            with self.subTest(language=language):
                response=self.client.get("/admin/backups/recoverycollectionreview/",HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(response.status_code,200,response.content[:500])
                self.assertContains(response,label)
                self.assertContains(response,str(capture.operation_id))
        decision=backups.RecoveryCollectionReviewDecision.objects.get()
        self.assertEqual(self.client.get("/admin/backups/recoverycollectionreviewdecision/").status_code,200)
        self.assertEqual(self.client.post(f"/admin/backups/recoverycollectionreviewdecision/{decision.id}/change/",{"reason":"Écrasement"}).status_code,403)
        self.assertEqual(self.client.get("/admin/backups/recoverycollectionreview/add/").status_code,403)
        self.owner.is_staff=True;self.owner.save();self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/admin/backups/recoverycollectionreview/").status_code,403)
        self.client.force_login(self.technical);self.technical.is_superuser=False;self.technical.save()
        self.assertEqual(self.client.get("/admin/backups/recoverycollectionreviewdecision/").status_code,403)
