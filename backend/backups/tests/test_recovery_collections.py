"""Recovered real collections are reviewed once without silently reallocating money."""
from copy import deepcopy
from datetime import datetime,timezone
import uuid
from django.test import TestCase
from django.db import connection,IntegrityError
from backups import models as backups
from backups.tests import test_recovery_journal_api as fixtures
from backups.tests import test_recovery_collection_snapshot as snapshot_fixtures
from common.models import Activity
from customers.models import Customer,CustomerDebt,CustomerPayment,CustomerLedgerEntry
from cash.models import CashMovement
from stock.models import StockMovement
from identity.models import User,DeviceSession,Membership


class RecoveryCollectionTests(TestCase):
    authenticate=fixtures.RecoveryJournalApiTests.authenticate
    generation=fixtures.RecoveryJournalApiTests.generation
    record=fixtures.RecoveryJournalApiTests.record
    device=fixtures.RecoveryJournalApiTests.device
    review=fixtures.RecoveryJournalApiTests.review
    payload=fixtures.RecoveryJournalApiTests.payload
    url=fixtures.RecoveryJournalApiTests.url

    def setUp(self):
        fixtures.RecoveryJournalApiTests.setUp(self)
        self.customer=Customer.objects.create(shop=self.a,name="Client A")
        self.live=DeviceSession.objects.filter(installation__user=self.owner).latest("created_at")
        self.debts=[]
        for amount in (3000,7000):
            event=Activity.objects.create(shop=self.a,actor=self.owner,action="customer_debt_opened",target_id=uuid.uuid4(),reason="Ouverture connue")
            self.debts.append(CustomerDebt.objects.create(id=event.target_id,shop=self.a,customer=self.customer,actor=self.owner,
                installation=self.live.installation,operation=event,amount_fcfa=amount,remaining_fcfa=amount,
                occurred_at=datetime(2026,10,20,9,tzinfo=timezone.utc)))

    def source(self):
        source=snapshot_fixtures.RecoveryCollectionSnapshotTests().source()
        parts=[{"id":str(uuid.uuid4()),"method":method,"amount_fcfa":amount,"other_name":"","external_reference":""}
            for method,amount in (("cash",2000),("orange",3000))]
        allocations=[{"id":str(uuid.uuid4()),"debt_id":str(debt.id),"amount_fcfa":amount} for debt,amount in zip(self.debts,(3000,2000),strict=True)]
        source.payload["original_request"].update(customer_id=str(self.customer.id),parts=deepcopy(parts),allocations=allocations)
        source.payload["collection"].update(customer_id=str(self.customer.id),amount_fcfa=5000,unallocated_fcfa=0,
            requires_regularization=False,parts=[{**part,"received_fcfa":None,"returned_fcfa":None} for part in parts],
            allocations=[{"id":row["id"],"debt_id":row["debt_id"],"requested_amount_fcfa":row["amount_fcfa"],"applied_amount_fcfa":row["amount_fcfa"]} for row in allocations])
        return source

    def capture(self,source,**changes):
        data=self.payload();entry=data["operations"][0]
        data["operations"]=[{**entry,"operation_type":"customer_collection","operation_id":str(source.operation_id),
            "origin_user_id":str(source.origin_user_id),"origin_installation_id":str(source.origin_installation_id),
            "occurred_at":source.occurred_at.isoformat(),"payload":source.payload,"dependencies":[],**changes}]
        response=self.client.post(self.url(),data,format="json");self.assertEqual(response.status_code,201,response.content)
        return backups.RecoveryJournalOperation.objects.get(id=response.data["operations"][0]["capture_id"])

    def preview(self,capture):
        response=self.client.get(self.url(f"operations/{capture.id}/integration-preview/"))
        self.assertEqual(response.status_code,200,response.content);return response.data

    def apply(self,capture,preview=None,**changes):
        preview=preview or self.preview(capture)
        body={"operation_id":str(uuid.uuid4()),"previous_version":preview["version"],"preview_token":preview["preview_token"],
            "reason":"Encaissement et copie d'origine vérifiés",**changes}
        return self.client.post(self.url(f"operations/{capture.id}/integrate/"),body,format="json"),body

    def test_collection_changes_only_cash_debt_and_ledger_once_and_keeps_historical_origin(self):
        source=self.source();capture=self.capture(source);users=User.objects.count()
        preview=self.preview(capture)
        self.assertTrue(preview["can_apply"])
        self.assertEqual(preview["cash_delta_by_method_fcfa"],{"cash":2000,"orange":3000})
        self.assertEqual(CustomerPayment.objects.count(),0)
        response,command=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)
        payment=CustomerPayment.objects.get()
        self.assertEqual((payment.id,payment.amount_fcfa,payment.unallocated_fcfa),(uuid.UUID(source.payload["collection"]["id"]),5000,0))
        self.assertEqual(payment.occurred_at,source.occurred_at)
        self.assertEqual(payment.actor_id,self.owner.id)
        self.assertEqual(response.data["business_receipt"]["actor_id"],str(source.origin_user_id))
        self.assertEqual(response.data["business_receipt"]["installation_id"],str(source.origin_installation_id))
        self.assertEqual(list(CustomerDebt.objects.filter(id__in=[d.id for d in self.debts]).order_by("amount_fcfa").values_list("remaining_fcfa",flat=True)),[0,5000])
        self.assertEqual(sorted(CashMovement.objects.values_list("amount_fcfa",flat=True)),[2000,3000])
        self.assertEqual(StockMovement.objects.count(),0)
        self.assertEqual(User.objects.count(),users)
        ledger=CustomerLedgerEntry.objects.get(operation_id=source.operation_id)
        self.assertEqual((ledger.delta_fcfa,ledger.debt_delta_fcfa,ledger.credit_delta_fcfa),(-5000,-5000,0))
        repeated=self.client.post(self.url(f"operations/{capture.id}/integrate/"),command,format="json")
        self.assertEqual((repeated.status_code,repeated.data),(200,response.data))
        known=self.preview(capture);self.assertEqual(known["outcome"],"already_known")
        self.assertEqual(known["cash_delta_by_method_fcfa"],{})
        again,_=self.apply(capture,known);self.assertEqual(again.status_code,201,again.content)
        self.assertEqual((CustomerPayment.objects.count(),CashMovement.objects.count(),CustomerLedgerEntry.objects.count()),(1,2,1))

    def test_original_unapplied_money_never_auto_settles_a_restored_debt(self):
        source=self.source();source.payload["original_request"]["allocations"]=source.payload["original_request"]["allocations"][:1]
        snapshot=source.payload["collection"];snapshot["allocations"]=snapshot["allocations"][:1]
        snapshot["allocations"][0]["applied_amount_fcfa"]=0;snapshot.update(unallocated_fcfa=5000,requires_regularization=True)
        capture=self.capture(source);preview=self.preview(capture)
        self.assertEqual(preview["new_customer_credit_fcfa"],5000)
        response,_=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(list(CustomerDebt.objects.filter(customer=self.customer).order_by("amount_fcfa").values_list("remaining_fcfa",flat=True)),[3000,7000])
        payment=CustomerPayment.objects.get();self.assertEqual(payment.unallocated_fcfa,5000);self.assertTrue(payment.requires_regularization)
        self.assertEqual(payment.allocations.get().requested_amount_fcfa,3000)
        self.assertEqual(payment.allocations.get().applied_amount_fcfa,0)

    def test_stale_preview_requires_review_again_and_retains_all_real_money_as_credit(self):
        source=self.source();capture=self.capture(source);initial=self.preview(capture)
        CustomerDebt.objects.filter(id=self.debts[0].id).update(remaining_fcfa=1000)
        CustomerDebt.objects.filter(id=self.debts[1].id).update(remaining_fcfa=0)
        stale,_=self.apply(capture,initial);self.assertEqual(stale.status_code,409,stale.content)
        self.assertEqual(CustomerPayment.objects.count(),0)
        latest=self.preview(capture);self.assertEqual(latest["new_customer_credit_fcfa"],4000)
        response,_=self.apply(capture,latest);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual((CustomerPayment.objects.get().amount_fcfa,CustomerPayment.objects.get().unallocated_fcfa),(5000,4000))
        self.assertEqual(sum(CashMovement.objects.values_list("amount_fcfa",flat=True)),5000)

    def test_missing_debt_and_dependencies_remain_captured_without_effect(self):
        source=self.source();missing=uuid.uuid4()
        source.payload["original_request"]["allocations"][0]["debt_id"]=str(missing)
        source.payload["collection"]["allocations"][0]["debt_id"]=str(missing)
        capture=self.capture(source,dependencies=[str(uuid.uuid4())]);preview=self.preview(capture)
        self.assertFalse(preview["can_apply"])
        self.assertEqual({row["code"] for row in preview["issues"]},{"missing_debt","missing_dependencies"})
        response,_=self.apply(capture,preview);self.assertEqual(response.status_code,409)
        self.assertEqual((CustomerPayment.objects.count(),CashMovement.objects.count()),(0,0))
        self.assertTrue(backups.RecoveryJournalOperation.objects.filter(id=capture.id).exists())

    def test_a_native_collection_cannot_bypass_review_with_the_captured_operation_uuid(self):
        source=self.source();self.capture(source)
        data={key:value for key,value in source.payload["original_request"].items() if key not in ("customer_id","installation_id")}
        response=self.client.post(f"/api/v1/shops/{self.a.id}/clients/{self.customer.id}/payments/",data,format="json")
        self.assertEqual(response.status_code,409,response.content)
        self.assertEqual((CustomerPayment.objects.count(),CashMovement.objects.count()),(0,0))

    def test_review_command_cannot_reuse_a_source_or_existing_business_operation_uuid(self):
        source=self.source();capture=self.capture(source);preview=self.preview(capture)
        for operation_id in (source.operation_id,self.debts[0].operation_id):
            with self.subTest(operation_id=operation_id):
                response,_=self.apply(capture,preview,operation_id=str(operation_id))
                self.assertEqual(response.status_code,409,response.content)
                self.assertEqual(CustomerPayment.objects.count(),0)

    def test_native_collection_cannot_reuse_an_integration_decision_uuid(self):
        source=self.source();capture=self.capture(source)
        response,command=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        data={"id":str(uuid.uuid4()),"operation_id":command["operation_id"],"allocations":[],
            "parts":[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":1000}]}
        native=self.client.post(f"/api/v1/shops/{self.a.id}/clients/{self.customer.id}/payments/",data,format="json")
        self.assertEqual(native.status_code,409,native.content)
        self.assertEqual((CustomerPayment.objects.count(),CashMovement.objects.count()),(1,2))

    def test_late_decision_failure_rolls_back_cash_allocations_debts_origin_and_ledger(self):
        capture=self.capture(self.source());preview=self.preview(capture)
        with connection.cursor() as cursor:
            cursor.execute("CREATE TRIGGER fail_collection_review BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected late decision failure'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(capture,preview)
        finally:
            with connection.cursor() as cursor:cursor.execute("DROP TRIGGER fail_collection_review")
        self.assertEqual((CustomerPayment.objects.count(),CashMovement.objects.count(),CustomerLedgerEntry.objects.count(),
            backups.RecoveredOperationOrigin.objects.count(),backups.RecoveryIntegrationDecision.objects.count()),(0,0,0,0,0))
        self.assertFalse(Activity.objects.filter(id=capture.operation_id).exists())
        self.assertEqual(list(CustomerDebt.objects.filter(customer=self.customer).order_by("amount_fcfa").values_list("remaining_fcfa",flat=True)),[3000,7000])
        self.assertEqual(self.preview(capture)["preview_token"],preview["preview_token"])
        response,_=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)

    def test_existing_native_collection_is_recognized_from_initial_receipt(self):
        from backups.services.recovery_collection_snapshot import immutable_collection_receipt
        source=self.source();source.origin_user_id=self.owner.id;source.origin_installation_id=self.live.installation_id
        raw=source.payload["original_request"]
        raw["installation_id"]=str(source.origin_installation_id)
        data={key:value for key,value in raw.items() if key not in ("customer_id","installation_id")}
        response=self.client.post(f"/api/v1/shops/{self.a.id}/clients/{self.customer.id}/payments/",data,format="json")
        self.assertEqual(response.status_code,201,response.content)
        source.payload["collection"]=immutable_collection_receipt(response.data)
        source.occurred_at=CustomerPayment.objects.get().occurred_at
        capture=self.capture(source);preview=self.preview(capture)
        self.assertEqual((preview["outcome"],preview["cash_delta_by_method_fcfa"],preview["new_customer_credit_fcfa"]),("already_known",{},0))
        acknowledged,_=self.apply(capture,preview);self.assertEqual(acknowledged.status_code,201,acknowledged.content)
        self.assertEqual(backups.RecoveredOperationOrigin.objects.count(),0)
        self.assertEqual((CustomerPayment.objects.count(),CashMovement.objects.count()),(1,2))
        native_retry=self.client.post(f"/api/v1/shops/{self.a.id}/clients/{self.customer.id}/payments/",data,format="json")
        self.assertEqual((native_retry.status_code,native_retry.data),(200,response.data))

    def test_lower_recovered_allocations_and_later_refund_remain_known_in_new_generation(self):
        from customers.api.regularizations import regularize_payment,RefundUnallocatedSerializer
        source=self.source()
        CustomerDebt.objects.filter(customer=self.customer).update(remaining_fcfa=0)
        first=self.capture(source);response,_=self.apply(first);self.assertEqual(response.status_code,201,response.content)
        payment=CustomerPayment.objects.get();self.assertEqual(payment.unallocated_fcfa,5000)
        refund=RefundUnallocatedSerializer(data={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),
            "reason":"Remboursement effectivement remis","amount_fcfa":1000,"method":"cash"})
        refund.is_valid(raise_exception=True)
        regularize_payment(self.a.id,self.customer.id,payment.id,self.owner,self.live.installation,self.live.id,"refund",refund.validated_data)
        current=self.client.get(f"/api/v1/shops/{self.a.id}/clients/{self.customer.id}/payments/")
        self.assertEqual(current.status_code,200,current.content)
        self.assertEqual((current.data["results"][0]["actor_id"],current.data["results"][0]["unallocated_fcfa"]),(str(source.origin_user_id),4000))
        before=[model.objects.count() for model in (Activity,CustomerPayment,CashMovement,CustomerLedgerEntry,User)]
        self.current.current_slot=None;self.current.save();self.current=self.generation();self.registry=self.record(self.current)
        self.review(self.a,self.owner,"owner",confirmed=True)
        self.assertEqual(self.client.get(self.url(f"operations/{first.id}/integration-preview/")).status_code,404)
        second=self.capture(source);preview=self.preview(second)
        self.assertEqual((preview["outcome"],preview["cash_delta_by_method_fcfa"],preview["new_customer_credit_fcfa"]),("already_known",{},0))
        acknowledged,_=self.apply(second,preview);self.assertEqual(acknowledged.status_code,201,acknowledged.content)
        self.assertEqual(acknowledged.data["business_receipt"],response.data["business_receipt"])
        self.assertEqual([model.objects.count() for model in (Activity,CustomerPayment,CashMovement,CustomerLedgerEntry,User)],before)
        self.assertEqual(backups.RecoveredOperationOrigin.objects.count(),1)
        self.assertEqual(backups.RecoveryIntegrationDecision.objects.count(),2)
        payment.refresh_from_db();self.assertEqual(payment.unallocated_fcfa,4000)

    def test_foreign_debt_customer_and_changed_source_identity_do_not_leak_balances(self):
        foreign=Customer.objects.create(shop=self.b,name="Client privé B")
        event=Activity.objects.create(shop=self.b,actor=self.owner,action="customer_debt_opened",target_id=uuid.uuid4(),reason="Justificatif privé B")
        debt=CustomerDebt.objects.create(id=event.target_id,shop=self.b,customer=foreign,actor=self.owner,
            installation=self.live.installation,operation=event,amount_fcfa=987654321,remaining_fcfa=987654321,
            occurred_at=self.debts[0].occurred_at)
        source=self.source()
        source.payload["original_request"]["allocations"][0]["debt_id"]=str(debt.id)
        source.payload["collection"]["allocations"][0]["debt_id"]=str(debt.id)
        source.origin_installation_id=self.live.installation_id
        source.payload["original_request"]["installation_id"]=str(self.live.installation_id)
        capture=self.capture(source);preview=self.preview(capture)
        self.assertEqual({row["code"] for row in preview["issues"]},{"missing_debt","origin_identity_conflict"})
        import json
        serialized=json.dumps(preview)
        for private in ("987654321","Client privé B","Justificatif privé B",str(event.id)):
            self.assertNotIn(private,serialized)
        response,_=self.apply(capture,preview);self.assertEqual(response.status_code,409)
        self.assertEqual(CustomerPayment.objects.count(),0)

    def test_current_rights_removed_after_preview_block_the_first_application(self):
        capture=self.capture(self.source());preview=self.preview(capture)
        review=backups.RestoreAccessReview.objects.get(generation=self.current,shop=self.a,user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=1,version=2,state="refused",reason="Droits actuels non confirmés")
        response,_=self.apply(capture,preview);self.assertEqual(response.status_code,404,response.content)
        self.assertEqual((CustomerPayment.objects.count(),CashMovement.objects.count()),(0,0))

    def test_invalid_source_has_a_translated_error_without_destroying_the_copy(self):
        source=self.source();source.payload["collection"]["amount_fcfa"]=4999;capture=self.capture(source)
        for language,message in (("fr","Les données figées de l'encaissement récupéré sont incohérentes."),
                ("en","Recovery collection snapshot is inconsistent.")):
            with self.subTest(language=language):
                response=self.client.get(self.url(f"operations/{capture.id}/integration-preview/"),HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(response.status_code,400,response.content)
                self.assertEqual(str(response.data["detail"]),message)
        self.assertTrue(backups.RecoveryJournalOperation.objects.filter(id=capture.id).exists())
        self.assertEqual(CustomerPayment.objects.count(),0)

    def test_current_rights_are_required_again_after_preview_and_on_exact_replay(self):
        capture=self.capture(self.source());preview=self.preview(capture)
        response,command=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)
        review=backups.RestoreAccessReview.objects.get(generation=self.current,shop=self.a,user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=1,version=2,state="refused",reason="Contrôle actuel retiré")
        repeated=self.client.post(self.url(f"operations/{capture.id}/integrate/"),command,format="json")
        self.assertEqual(repeated.status_code,404,repeated.content)
        self.assertEqual((CustomerPayment.objects.count(),CashMovement.objects.count()),(1,2))

    def test_collection_preview_receipt_and_errors_match_openapi(self):
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
        capture=self.capture(self.source());path=self.url(f"operations/{capture.id}/")
        preview=self.client.get(path+"integration-preview/");check(preview,"/integration-preview/")
        response,command=self.apply(capture,preview.data);self.assertEqual(response.status_code,201,response.content);check(response,"/integrate/","post")
        check(self.client.post(path+"integrate/",command,format="json"),"/integrate/","post")
        check(self.client.post(path+"integrate/",{**command,"reason":"Changed"},format="json"),"/integrate/","post")
