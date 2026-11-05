"""Recover a missing customer then opening debt before its real collection."""
from datetime import datetime,timedelta,timezone
from copy import deepcopy
import uuid
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection,IntegrityError
from backups import models as backups
from backups.tests import test_recovery_journal_api as fixtures
from backups.tests import test_recovery_customer_snapshots as snapshots
from backups.tests import test_recovery_collection_snapshot as collections
from common.models import Activity
from customers.models import Customer,CustomerDebt,CustomerPayment,CustomerLedgerEntry
from cash.models import CashMovement
from stock.models import StockMovement
from sales.models import Sale
from identity.models import User,DeviceSession,Membership


class RecoveryCustomerDependencyTests(TestCase):
    setUp=fixtures.RecoveryJournalApiTests.setUp
    authenticate=fixtures.RecoveryJournalApiTests.authenticate
    generation=fixtures.RecoveryJournalApiTests.generation
    record=fixtures.RecoveryJournalApiTests.record
    device=fixtures.RecoveryJournalApiTests.device
    review=fixtures.RecoveryJournalApiTests.review
    payload=fixtures.RecoveryJournalApiTests.payload
    url=fixtures.RecoveryJournalApiTests.url

    def customer_source(self):return snapshots.RecoveryCustomerSnapshotTests().customer_source()
    def debt_source(self,customer_id):return snapshots.RecoveryCustomerSnapshotTests().debt_source(customer_id)

    def capture(self,source,dependencies=None):
        data=self.payload();entry=data["operations"][0]
        data["operations"]=[{**entry,"operation_id":str(source.operation_id),"operation_type":source.operation_type,
            "origin_user_id":str(source.origin_user_id),"origin_installation_id":str(source.origin_installation_id),
            "occurred_at":source.occurred_at.isoformat(),"origin_recorded_at":(source.occurred_at+timedelta(minutes=1)).isoformat(),
            "payload":source.payload,"dependencies":[str(value) for value in dependencies or []]}]
        response=self.client.post(self.url(),data,format="json");self.assertEqual(response.status_code,201,response.content)
        return backups.RecoveryJournalOperation.objects.get(id=response.data["operations"][0]["capture_id"])

    def preview(self,capture):
        response=self.client.get(self.url(f"operations/{capture.id}/integration-preview/"))
        self.assertEqual(response.status_code,200,response.content);return response.data

    def apply(self,capture,preview=None,**changes):
        preview=preview or self.preview(capture)
        command={"operation_id":str(uuid.uuid4()),"previous_version":preview["version"],"preview_token":preview["preview_token"],
            "reason":"Fiche et faits d'ouverture vérifiés",**changes}
        return self.client.post(self.url(f"operations/{capture.id}/integrate/"),command,format="json"),command

    def test_customer_opening_and_collection_recover_in_dependency_order_without_fictitious_sale_or_cash(self):
        customer=self.customer_source();customer_id=customer.payload["customer"]["id"]
        debt=self.debt_source(customer_id)
        payment=collections.RecoveryCollectionSnapshotTests().source();payment.operation_type="customer_collection"
        for raw in (payment.payload["original_request"],payment.payload["collection"]):raw["customer_id"]=customer_id
        for row in (payment.payload["original_request"]["allocations"][0],payment.payload["collection"]["allocations"][0]):
            row["debt_id"]=debt.payload["opening_debt"]["id"]
        client_capture=self.capture(customer)
        debt_capture=self.capture(debt,[customer.operation_id])
        payment_capture=self.capture(payment,[customer.operation_id,debt.operation_id])
        old_debt=self.preview(debt_capture);old_payment=self.preview(payment_capture)
        self.assertFalse(old_debt["can_apply"]);self.assertFalse(old_payment["can_apply"])
        users=User.objects.count()
        response,_=self.apply(client_capture);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(User.objects.count(),users)
        self.assertEqual((CustomerDebt.objects.count(),CashMovement.objects.count(),CustomerLedgerEntry.objects.count()),(0,0,0))
        self.assertEqual(self.apply(debt_capture,old_debt)[0].status_code,409)
        response,_=self.apply(debt_capture);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual((CustomerDebt.objects.get().amount_fcfa,CustomerDebt.objects.get().remaining_fcfa),(8000,8000))
        self.assertEqual((CashMovement.objects.count(),Sale.objects.count(),StockMovement.objects.count()),(0,0,0))
        self.assertEqual(self.apply(payment_capture,old_payment)[0].status_code,409)
        response,command=self.apply(payment_capture);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual((CustomerPayment.objects.get().amount_fcfa,CustomerPayment.objects.get().unallocated_fcfa),(3000,1000))
        self.assertEqual(CustomerDebt.objects.get().remaining_fcfa,6000)
        self.assertEqual(sum(CashMovement.objects.values_list("amount_fcfa",flat=True)),3000)
        self.assertEqual(sorted(CustomerLedgerEntry.objects.values_list("delta_fcfa",flat=True)),[-3000,8000])
        self.assertEqual(backups.RecoveredOperationOrigin.objects.count(),3)
        history=self.client.get(f"/api/v1/shops/{self.a.id}/clients/{customer_id}/history/")
        self.assertEqual(history.data["results"][0]["actor_id"],str(customer.origin_user_id))
        history=self.client.get(f"/api/v1/shops/{self.a.id}/clients/{customer_id}/debts/{debt.payload['opening_debt']['id']}/history/")
        self.assertEqual(history.data["results"][0]["actor_id"],str(debt.origin_user_id))
        self.assertEqual(self.client.post(self.url(f"operations/{payment_capture.id}/integrate/"),command,format="json").status_code,200)

    def test_equal_contact_keeps_distinct_customer_ids_dates_and_initial_receipt(self):
        source=self.customer_source();snapshot=source.payload["customer"]
        existing=Customer.objects.create(shop=self.a,name=snapshot["name"],phone=snapshot["phone"])
        capture=self.capture(source);preview=self.preview(capture)
        self.assertTrue(preview["has_possible_duplicate"])
        response,command=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)
        recovered=Customer.objects.get(id=snapshot["id"])
        self.assertEqual(recovered.created_at,source.occurred_at)
        self.assertEqual(Customer.objects.filter(shop=self.a).count(),2)
        self.assertTrue(response.data["business_receipt"]["has_possible_duplicate"])
        self.assertNotEqual(recovered.id,existing.id)
        self.assertEqual(self.preview(capture)["outcome"],"already_known")
        self.assertEqual(self.client.post(self.url(f"operations/{capture.id}/integrate/"),command,format="json").data,response.data)
        self.assertEqual((CashMovement.objects.count(),CustomerLedgerEntry.objects.count()),(0,0))

    def test_customer_duplicate_hint_change_requires_a_new_preview(self):
        source=self.customer_source();capture=self.capture(source);preview=self.preview(capture)
        Customer.objects.create(shop=self.a,name=source.payload["customer"]["name"])
        response,_=self.apply(capture,preview);self.assertEqual(response.status_code,409,response.content)
        self.assertEqual(Customer.objects.count(),1)
        self.assertTrue(self.preview(capture)["has_possible_duplicate"])

    def test_opening_reference_in_another_shop_never_exposes_private_customer_data(self):
        foreign=Customer.objects.create(shop=self.b,name="Identité privée B",phone="+237600000099")
        capture=self.capture(self.debt_source(foreign.id));preview=self.preview(capture)
        self.assertFalse(preview["can_apply"])
        self.assertEqual({row["code"] for row in preview["issues"]},{"missing_customer"})
        import json
        self.assertNotIn("Identité privée B",json.dumps(preview));self.assertNotIn("+237600000099",json.dumps(preview))
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        self.assertEqual(CustomerDebt.objects.count(),0)

    def test_decision_failure_rolls_back_new_customer_and_opening_ledger(self):
        source=self.customer_source();capture=self.capture(source)
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER fail_customer_integration BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected integration failure'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(capture)
        finally:
            with connection.cursor() as cursor:cursor.execute("DROP TRIGGER fail_customer_integration")
        self.assertEqual((Customer.objects.count(),backups.RecoveredOperationOrigin.objects.count()),(0,0))
        self.assertFalse(Activity.objects.filter(id=source.operation_id).exists())
        self.assertEqual(self.apply(capture)[0].status_code,201)
        debt=self.debt_source(source.payload["customer"]["id"]);capture=self.capture(debt)
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TRIGGER fail_opening_integration BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected opening failure'); END")
        try:
            with self.assertRaises(IntegrityError):self.apply(capture)
        finally:
            with connection.cursor() as cursor:cursor.execute("DROP TRIGGER fail_opening_integration")
        self.assertEqual((CustomerDebt.objects.count(),CustomerLedgerEntry.objects.count(),CashMovement.objects.count()),(0,0,0))
        self.assertEqual(backups.RecoveredOperationOrigin.objects.count(),1)

    def test_native_customer_keeps_its_initial_receipt_after_correction_and_legacy_device_proof_is_explicit(self):
        source=self.customer_source();raw=source.payload["original_request"]
        native=self.client.post(f"/api/v1/shops/{self.a.id}/clients/",raw,format="json")
        self.assertEqual(native.status_code,201,native.content)
        live=DeviceSession.objects.get(installation__user=self.owner)
        event=Activity.objects.get(id=source.operation_id)
        self.assertEqual(event.details["installation_id"],str(live.installation_id))
        source.origin_user_id=self.owner.id;source.origin_installation_id=live.installation_id
        source.occurred_at=Customer.objects.get(id=native.data["id"]).created_at
        source.payload["customer"]={key:native.data[key] for key in ("id","name","phone","version","created_at","updated_at")}
        corrected=self.client.patch(f"/api/v1/shops/{self.a.id}/clients/{native.data['id']}/",{
            "operation_id":str(uuid.uuid4()),"base_version":1,"reason":"Nom vérifié plus tard","name":"Nom corrigé"},format="json")
        self.assertEqual(corrected.status_code,200,corrected.content)
        capture=self.capture(source);preview=self.preview(capture)
        self.assertEqual(preview["outcome"],"already_known");self.assertTrue(preview["origin_installation_verified"])
        event.details.pop("installation_id");event.save(update_fields=["details","updated_at"])
        legacy=self.preview(capture)
        self.assertTrue(legacy["can_apply"]);self.assertFalse(legacy["origin_installation_verified"])
        self.assertEqual({row["code"] for row in legacy["notices"]},{"legacy_origin_installation_unknown"})
        response,_=self.apply(capture,legacy);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.data["business_receipt"],native.data)
        current=Customer.objects.get(id=native.data["id"]);self.assertEqual((current.name,current.version),("Nom corrigé",2))
        event.refresh_from_db();self.assertNotIn("installation_id",event.details)
        self.assertEqual(backups.RecoveredOperationOrigin.objects.count(),0)

    def test_known_opening_uses_initial_balance_and_deadline_after_collection_and_due_change(self):
        customer=Customer.objects.create(shop=self.a,name="Client natif")
        source=self.debt_source(customer.id);live=DeviceSession.objects.get(installation__user=self.owner)
        source.origin_user_id=self.owner.id;source.origin_installation_id=live.installation_id
        source.payload["original_request"]["installation_id"]=str(live.installation_id)
        raw={key:value for key,value in source.payload["original_request"].items() if key not in ("customer_id","installation_id")}
        native=self.client.post(f"/api/v1/shops/{self.a.id}/clients/{customer.id}/debts/",raw,format="json")
        self.assertEqual(native.status_code,201,native.content)
        source.payload["opening_debt"]={key:native.data[key] for key in ("id","customer_id","sale_id","amount_fcfa","remaining_fcfa","occurred_at","due_at","due_version")}
        debt_id=native.data["id"]
        payment=self.client.post(f"/api/v1/shops/{self.a.id}/clients/{customer.id}/payments/",{
            "id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"parts":[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":3000}],
            "allocations":[{"id":str(uuid.uuid4()),"debt_id":debt_id,"amount_fcfa":3000}]},format="json")
        self.assertEqual(payment.status_code,201,payment.content)
        changed=self.client.post(f"/api/v1/shops/{self.a.id}/clients/{customer.id}/debts/{debt_id}/due-date/",{
            "operation_id":str(uuid.uuid4()),"base_version":1,"reason":"Nouvel accord explicite","due_date":"2026-11-02"},format="json")
        self.assertEqual(changed.status_code,200,changed.content)
        capture=self.capture(source);preview=self.preview(capture)
        self.assertEqual((preview["outcome"],preview["new_debt_fcfa"]),("already_known",0))
        response,_=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.data["business_receipt"],native.data)
        current=CustomerDebt.objects.get(id=debt_id);self.assertEqual((current.remaining_fcfa,current.due_version),(5000,2))
        self.assertEqual((CustomerPayment.objects.count(),CashMovement.objects.count()),(1,1))

    def test_recovered_dependencies_are_known_in_another_generation_after_customer_correction(self):
        source=self.customer_source();first=self.capture(source)
        client_receipt,_=self.apply(first);self.assertEqual(client_receipt.status_code,201,client_receipt.content)
        customer_id=source.payload["customer"]["id"]
        debt=self.debt_source(customer_id);opening=self.capture(debt,[source.operation_id])
        debt_receipt,_=self.apply(opening);self.assertEqual(debt_receipt.status_code,201,debt_receipt.content)
        corrected=self.client.patch(f"/api/v1/shops/{self.a.id}/clients/{customer_id}/",{
            "operation_id":str(uuid.uuid4()),"base_version":1,"name":"Nom actuel corrigé","reason":"Correction vérifiée"},format="json")
        self.assertEqual(corrected.status_code,200,corrected.content)
        before=[model.objects.count() for model in (Activity,Customer,CustomerDebt,CustomerLedgerEntry,User)]
        self.current.current_slot=None;self.current.save();self.current=self.generation();self.registry=self.record(self.current)
        self.review(self.a,self.owner,"owner",confirmed=True)
        for item,receipt in ((source,client_receipt),(debt,debt_receipt)):
            with self.subTest(operation_type=item.operation_type):
                capture=self.capture(item);preview=self.preview(capture)
                self.assertEqual((preview["outcome"],preview["new_debt_fcfa"]),("already_known",0))
                acknowledged,_=self.apply(capture,preview)
                self.assertEqual(acknowledged.status_code,201,acknowledged.content)
                self.assertEqual(acknowledged.data["business_receipt"],receipt.data["business_receipt"])
        self.assertEqual([model.objects.count() for model in (Activity,Customer,CustomerDebt,CustomerLedgerEntry,User)],before)
        self.assertEqual(Customer.objects.get().name,"Nom actuel corrigé")
        self.assertEqual(backups.RecoveredOperationOrigin.objects.count(),2)

    def test_customer_and_opening_native_requests_cannot_bypass_captured_uuid_and_existing_entities_are_not_overwritten(self):
        source=self.customer_source();capture=self.capture(source)
        native=self.client.post(f"/api/v1/shops/{self.a.id}/clients/",source.payload["original_request"],format="json")
        self.assertEqual(native.status_code,409,native.content)
        self.assertEqual(Customer.objects.count(),0)
        existing=Customer.objects.create(id=source.payload["customer"]["id"],shop=self.a,name="Fiche existante conservée")
        preview=self.preview(capture);self.assertFalse(preview["can_apply"])
        self.assertEqual({row["code"] for row in preview["issues"]},{"entity_uuid_conflict"})
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        existing.refresh_from_db();self.assertEqual(existing.name,"Fiche existante conservée")
        debt=self.debt_source(existing.id);debt.origin_user_id=self.owner.id
        live=DeviceSession.objects.get(installation__user=self.owner);debt.origin_installation_id=live.installation_id
        debt.payload["original_request"]["installation_id"]=str(live.installation_id)
        self.capture(debt)
        raw={key:value for key,value in debt.payload["original_request"].items() if key not in ("customer_id","installation_id")}
        native=self.client.post(f"/api/v1/shops/{self.a.id}/clients/{existing.id}/debts/",raw,format="json")
        self.assertEqual(native.status_code,409,native.content)
        self.assertEqual((CustomerDebt.objects.count(),CashMovement.objects.count()),(0,0))

    def test_changed_source_identity_or_creation_fields_in_another_generation_do_not_rewrite_known_customer(self):
        source=self.customer_source();response,_=self.apply(self.capture(source));self.assertEqual(response.status_code,201,response.content)
        self.current.current_slot=None;self.current.save();self.current=self.generation();self.registry=self.record(self.current)
        self.review(self.a,self.owner,"owner",confirmed=True)
        changed=deepcopy(source);changed.payload["original_request"]["name"]="Fausse création"
        changed.payload["customer"]["name"]="Fausse création"
        capture=self.capture(changed);preview=self.preview(capture)
        self.assertFalse(preview["can_apply"])
        self.assertIn("operation_conflict",{row["code"] for row in preview["issues"]})
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        self.assertEqual(Customer.objects.get().name,"Client retrouvé")

    def test_current_roles_are_checked_before_source_parsing_and_localized_invalid_payload_is_preserved(self):
        source=self.customer_source();source.payload["customer"]["version"]=2;capture=self.capture(source)
        seller=User.objects.create_user(username="cannot-integrate-customer")
        membership=Membership.objects.create(shop=self.a,user=seller)
        self.review(self.a,seller,"seller",membership=membership,confirmed=True)
        api=self.authenticate(seller)
        self.assertEqual(api.generic("POST",self.url(f"operations/{capture.id}/integrate/"),b"invalid",content_type="application/json").status_code,403)
        for language,message in (("fr","Les données figées du client ou de la dette d'ouverture récupérés sont incohérentes."),
                ("en","Recovery customer or opening-debt snapshot is inconsistent.")):
            with self.subTest(language=language):
                response=self.client.get(self.url(f"operations/{capture.id}/integration-preview/"),HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(response.status_code,400,response.content);self.assertEqual(str(response.data["detail"]),message)
        self.assertTrue(backups.RecoveryJournalOperation.objects.filter(id=capture.id).exists())
        self.assertEqual(Customer.objects.count(),0)

    def test_real_new_customer_and_opening_previews_and_receipts_match_openapi(self):
        import json
        from drf_spectacular.generators import SchemaGenerator
        from jsonschema import Draft4Validator,FormatChecker
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
        source=self.customer_source()
        for item in (source,self.debt_source(source.payload["customer"]["id"])):
            with self.subTest(operation_type=item.operation_type):
                capture=self.capture(item)
                preview=self.client.get(self.url(f"operations/{capture.id}/integration-preview/"))
                self.assertEqual(preview.status_code,200,preview.content);check(preview,"/integration-preview/")
                response,command=self.apply(capture,preview.data);self.assertEqual(response.status_code,201,response.content)
                check(response,"/integrate/","post")
                check(self.client.post(self.url(f"operations/{capture.id}/integrate/"),command,format="json"),"/integrate/","post")

    def test_foreign_operation_is_scoped_before_loading_any_private_activity_details(self):
        import json
        for source in (self.customer_source(),self.debt_source(uuid.uuid4())):
            with self.subTest(operation_type=source.operation_type):
                Activity.objects.create(id=source.operation_id,shop=self.b,actor=self.owner,action="client_created",
                    target_id=uuid.uuid4(),reason="Justificatif privé B",details={"response":{"private_customer":"Identité privée B"}})
                capture=self.capture(source)
                with CaptureQueriesContext(connection) as queries:preview=self.preview(capture)
                sensitive=[row["sql"] for row in queries.captured_queries if '"core_activity"."details"' in row["sql"]
                    and source.operation_id.hex in row["sql"]]
                self.assertTrue(sensitive)
                self.assertTrue(all(self.a.id.hex in sql for sql in sensitive),sensitive)
                self.assertFalse(preview["can_apply"])
                self.assertIn("operation_conflict",{row["code"] for row in preview["issues"]})
                self.assertNotIn("Identité privée B",json.dumps(preview))
                self.assertEqual(self.apply(capture,preview)[0].status_code,409)

    def test_corrupt_foreign_origin_link_is_not_loaded_or_treated_as_native_evidence(self):
        source=self.customer_source();capture=self.capture(source)
        response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        live=DeviceSession.objects.get(installation__user=self.owner)
        deposit=backups.RecoveryJournalDeposit.objects.create(session=self.registry,shop=self.b,operation_id=uuid.uuid4(),
            submitted_by=self.owner,submitted_installation=live.installation,reason="Copie privée B",format_version=1,content_sha256="f"*64,receipt={})
        foreign=backups.RecoveryJournalOperation.objects.create(session=self.registry,shop=self.b,first_deposit=deposit,
            operation_id=uuid.uuid4(),origin_user_id=uuid.uuid4(),origin_installation_id=uuid.uuid4(),operation_type="customer_creation",
            occurred_at=source.occurred_at,origin_recorded_at=source.occurred_at,payload={"private":"Données privées B"},dependencies=[],content_sha256="e"*64)
        backups.RecoveredOperationOrigin.objects.filter(operation_id=source.operation_id).update(capture=foreign)
        with CaptureQueriesContext(connection) as queries:preview=self.preview(capture)
        payload_queries=[row["sql"] for row in queries.captured_queries if '"backups_recoveryjournaloperation"."payload"' in row["sql"]]
        self.assertTrue(payload_queries)
        self.assertTrue(all(self.a.id.hex in sql for sql in payload_queries),payload_queries)
        self.assertFalse(preview["can_apply"]);self.assertFalse(preview["origin_installation_verified"])
        self.assertIn("operation_conflict",{row["code"] for row in preview["issues"]})
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        history=self.client.get(f"/api/v1/shops/{self.a.id}/clients/{source.payload['customer']['id']}/history/")
        self.assertEqual(history.status_code,409,history.content)
        self.assertEqual(history.data["code"],"origin_reference_conflict")
        import json
        self.assertNotIn(str(foreign.origin_user_id),json.dumps(history.data))
