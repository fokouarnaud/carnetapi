"""Recovered contact/deadline revisions never roll back real money or later edits."""
from copy import deepcopy
from datetime import datetime,timezone
import uuid

from django.db import connection,IntegrityError
from django.test import TestCase,SimpleTestCase
from backups import models as backups
from backups.tests import test_recovery_customer_dependencies as fixtures
from common.models import Activity
from customers.models import Customer,CustomerDebt,CustomerLedgerEntry
from cash.models import CashMovement
from identity.models import DeviceSession,User,Membership


class RevisionSources:
    def contact_source(self,before):
        from types import SimpleNamespace
        operation,actor,device=[uuid.uuid4() for _ in range(3)]
        occurred=datetime(2026,10,23,10,tzinfo=timezone.utc)
        after={**deepcopy(before),"name":"Nom corrigé","version":before["version"]+1,"updated_at":occurred.isoformat()}
        return SimpleNamespace(operation_type="customer_update",operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=occurred,payload={"schema_version":1,
                "original_request":{"operation_id":str(operation),"client_id":before["id"],"base_version":before["version"],
                    "reason":"Identité corrigée après contrôle","name":"Nom corrigé"},"customer_update":{"before":before,"after":after}})

    def deadline_source(self,customer_id,debt_id):
        from types import SimpleNamespace
        operation,actor,device=[uuid.uuid4() for _ in range(3)]
        occurred=datetime(2026,10,23,11,tzinfo=timezone.utc)
        return SimpleNamespace(operation_type="customer_due_change",operation_id=operation,origin_user_id=actor,
            origin_installation_id=device,occurred_at=occurred,payload={"schema_version":1,
                "original_request":{"operation_id":str(operation),"debt_id":str(debt_id),"installation_id":str(device),
                    "base_version":1,"reason":"Report accepté avec le client","due_date":"2026-11-04"},
                "due_change":{"id":str(debt_id),"customer_id":str(customer_id),"before_due_at":"2026-10-30T23:00:00Z",
                    "after_due_at":"2026-11-03T23:00:00Z","before_version":1,"after_version":2,"occurred_at":occurred.isoformat()}})


class RecoveryCustomerRevisionSnapshotTests(SimpleTestCase,RevisionSources):
    def before(self):
        return {"id":str(uuid.uuid4()),"name":"Nom initial","phone":None,"version":1,
            "created_at":"2026-10-22T09:00:00Z","updated_at":"2026-10-22T09:00:00Z"}

    def test_contact_request_only_changes_declared_fields_and_increments_the_frozen_version(self):
        from backups.services.recovery_customer_revisions import decode_customer_revision
        source=self.contact_source(self.before());decoded=decode_customer_revision(source,timezone_name="Africa/Douala")
        self.assertEqual(decoded.snapshot["after"]["name"],"Nom corrigé")
        self.assertEqual(decoded.snapshot["after"]["phone"],decoded.snapshot["before"]["phone"])
        self.assertEqual(decoded.canonical_request["client_id"],source.payload["customer_update"]["before"]["id"])

    def test_undeclared_fields_versions_entity_dates_and_request_origins_cannot_be_rewritten(self):
        from backups.services.recovery_customer_revisions import decode_customer_revision,RecoveryCustomerRevisionInvalid
        for case in ("phone","name","version","base","id","created","date","device","bool"):
            with self.subTest(case=case):
                source=self.contact_source(self.before());delta=source.payload["customer_update"];raw=source.payload["original_request"]
                if case=="phone":delta["after"]["phone"]="+237600000099"
                elif case=="name":delta["after"]["name"]="Autre correction"
                elif case=="version":delta["after"]["version"]=3
                elif case=="base":raw["base_version"]=2
                elif case=="id":delta["after"]["id"]=str(uuid.uuid4())
                elif case=="created":delta["after"]["created_at"]="2026-10-21T09:00:00Z"
                elif case=="date":delta["after"]["updated_at"]="2026-10-24T10:00:00Z"
                elif case=="bool":raw["base_version"]=True
                else:raw["installation_id"]=str(uuid.uuid4())
                with self.assertRaises(RecoveryCustomerRevisionInvalid):decode_customer_revision(source,timezone_name="Africa/Douala")

    def test_deadline_change_is_a_versioned_delta_without_a_balance_snapshot(self):
        from backups.services.recovery_customer_revisions import decode_customer_revision,RecoveryCustomerRevisionInvalid
        source=self.deadline_source(uuid.uuid4(),uuid.uuid4())
        decoded=decode_customer_revision(source,timezone_name="Africa/Douala")
        self.assertEqual(decoded.snapshot["after_due_at"].isoformat(),"2026-11-03T23:00:00+00:00")
        for case in ("version","date","amount","naive","debt"):
            with self.subTest(case=case):
                changed=deepcopy(source);delta=changed.payload["due_change"]
                if case=="version":delta["after_version"]=3
                elif case=="date":delta["after_due_at"]="2026-11-04T00:00:00Z"
                elif case=="amount":delta["remaining_fcfa"]=8000
                elif case=="naive":delta["occurred_at"]="2026-10-23T11:00:00"
                else:changed.payload["original_request"]["debt_id"]=str(uuid.uuid4())
                with self.assertRaises(RecoveryCustomerRevisionInvalid):decode_customer_revision(changed,timezone_name="Africa/Douala")


class RecoveryCustomerRevisionTests(TestCase,RevisionSources):
    authenticate=fixtures.RecoveryCustomerDependencyTests.authenticate
    generation=fixtures.RecoveryCustomerDependencyTests.generation
    record=fixtures.RecoveryCustomerDependencyTests.record
    device=fixtures.RecoveryCustomerDependencyTests.device
    review=fixtures.RecoveryCustomerDependencyTests.review
    payload=fixtures.RecoveryCustomerDependencyTests.payload
    url=fixtures.RecoveryCustomerDependencyTests.url
    capture=fixtures.RecoveryCustomerDependencyTests.capture
    preview=fixtures.RecoveryCustomerDependencyTests.preview
    apply=fixtures.RecoveryCustomerDependencyTests.apply

    def setUp(self):
        fixtures.RecoveryCustomerDependencyTests.setUp(self)
        self.live=DeviceSession.objects.get(installation__user=self.owner)
        past=datetime(2026,10,22,9,tzinfo=timezone.utc)
        self.customer=Customer.objects.create(shop=self.a,name="Nom initial",phone="+237600000001")
        Customer.objects.filter(id=self.customer.id).update(created_at=past,updated_at=past);self.customer.refresh_from_db()
        event=Activity.objects.create(shop=self.a,actor=self.owner,action="customer_debt_opened",target_id=uuid.uuid4(),reason="Ouverture connue")
        self.debt=CustomerDebt.objects.create(id=event.target_id,shop=self.a,customer=self.customer,actor=self.owner,
            installation=self.live.installation,operation=event,amount_fcfa=8000,remaining_fcfa=5000,occurred_at=past,
            due_at=datetime(2026,10,30,23,tzinfo=timezone.utc))

    def before(self):
        return {"id":str(self.customer.id),"name":self.customer.name,"phone":self.customer.phone,"version":self.customer.version,
            "created_at":self.customer.created_at.isoformat(),"updated_at":self.customer.updated_at.isoformat()}

    def test_contact_and_deadline_recovery_keep_uuid_paid_balance_and_original_origin(self):
        contact=self.contact_source(self.before());deadline=self.deadline_source(self.customer.id,self.debt.id)
        response,_=self.apply(self.capture(contact));self.assertEqual(response.status_code,201,response.content)
        self.customer.refresh_from_db();self.assertEqual((self.customer.name,self.customer.version),("Nom corrigé",2))
        self.assertEqual(self.customer.updated_at,contact.occurred_at)
        response,_=self.apply(self.capture(deadline));self.assertEqual(response.status_code,201,response.content)
        self.debt.refresh_from_db()
        self.assertEqual((self.debt.remaining_fcfa,self.debt.amount_fcfa,self.debt.due_version),(5000,8000,2))
        self.assertEqual(self.debt.due_at,datetime(2026,11,3,23,tzinfo=timezone.utc))
        self.assertEqual(CashMovement.objects.count(),0)
        entry=CustomerLedgerEntry.objects.get(operation_id=deadline.operation_id)
        self.assertEqual((entry.delta_fcfa,entry.debt_delta_fcfa,entry.credit_delta_fcfa,entry.occurred_at),(0,0,0,deadline.occurred_at))
        self.assertEqual(backups.RecoveredOperationOrigin.objects.count(),2)

    def test_conflicting_current_versions_and_before_values_remain_captured_without_overwriting(self):
        source=self.contact_source(self.before());capture=self.capture(source)
        Customer.objects.filter(id=self.customer.id).update(name="Autre correction confirmée",version=2)
        preview=self.preview(capture);self.assertFalse(preview["can_apply"])
        self.assertIn("customer_revision_conflict",{row["code"] for row in preview["issues"]})
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        self.customer.refresh_from_db();self.assertEqual(self.customer.name,"Autre correction confirmée")
        deadline=self.deadline_source(self.customer.id,self.debt.id);capture=self.capture(deadline)
        CustomerDebt.objects.filter(id=self.debt.id).update(due_at=None)
        preview=self.preview(capture);self.assertFalse(preview["can_apply"])
        self.assertIn("debt_due_revision_conflict",{row["code"] for row in preview["issues"]})
        self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        self.assertEqual(CashMovement.objects.count(),0)

    def test_stale_deadline_preview_requires_review_but_never_resets_a_concurrently_settled_debt(self):
        source=self.deadline_source(self.customer.id,self.debt.id);capture=self.capture(source);old=self.preview(capture)
        CustomerDebt.objects.filter(id=self.debt.id).update(remaining_fcfa=0)
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        self.debt.refresh_from_db();self.assertEqual((self.debt.remaining_fcfa,self.debt.due_version),(0,2))
        self.assertEqual(CustomerLedgerEntry.objects.get().delta_fcfa,0)

    def test_late_failure_rolls_back_customer_and_deadline_mutations_and_their_history(self):
        for source in (self.contact_source(self.before()),self.deadline_source(self.customer.id,self.debt.id)):
            with self.subTest(operation_type=source.operation_type):
                capture=self.capture(source)
                with connection.cursor() as cursor:
                    cursor.execute("CREATE TEMP TRIGGER fail_revision_decision BEFORE INSERT ON backups_recoveryintegrationdecision BEGIN SELECT RAISE(ABORT,'injected revision failure'); END")
                try:
                    with self.assertRaises(IntegrityError):self.apply(capture)
                finally:
                    with connection.cursor() as cursor:cursor.execute("DROP TRIGGER fail_revision_decision")
                self.customer.refresh_from_db();self.debt.refresh_from_db()
                self.assertEqual((self.customer.name,self.customer.version,self.debt.due_version),("Nom initial",1,1))
                self.assertEqual((CustomerLedgerEntry.objects.count(),backups.RecoveredOperationOrigin.objects.count()),(0,0))
                self.assertFalse(Activity.objects.filter(id=source.operation_id).exists())

    def test_native_contact_revision_and_legacy_installation_evidence_are_recognized_without_reverting_later_name(self):
        source=self.contact_source(self.before());source.origin_user_id=self.owner.id;source.origin_installation_id=self.live.installation_id
        raw={key:value for key,value in source.payload["original_request"].items() if key!="client_id"}
        native=self.client.patch(f"/api/v1/shops/{self.a.id}/clients/{self.customer.id}/",raw,format="json")
        self.assertEqual(native.status_code,200,native.content)
        event=Activity.objects.get(id=source.operation_id);self.assertEqual(event.details["installation_id"],str(self.live.installation_id))
        source.payload["customer_update"]["after"]={key:native.data[key] for key in source.payload["customer_update"]["after"]}
        source.occurred_at=datetime.fromisoformat(native.data["updated_at"].replace('Z','+00:00'))
        later=self.client.patch(f"/api/v1/shops/{self.a.id}/clients/{self.customer.id}/",{
            "operation_id":str(uuid.uuid4()),"base_version":2,"name":"Modification ultérieure","reason":"Nouvelle correction autorisée"},format="json")
        self.assertEqual(later.status_code,200,later.content)
        capture=self.capture(source);preview=self.preview(capture)
        self.assertEqual(preview["outcome"],"already_known");self.assertTrue(preview["origin_installation_verified"])
        event.details.pop("installation_id");event.save(update_fields=["details","updated_at"])
        legacy=self.preview(capture);self.assertFalse(legacy["origin_installation_verified"])
        self.assertIn("legacy_origin_installation_unknown",{row["code"] for row in legacy["notices"]})
        response,_=self.apply(capture,legacy);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.data["business_receipt"],native.data)
        self.customer.refresh_from_db();self.assertEqual((self.customer.name,self.customer.version),("Modification ultérieure",3))
        event.refresh_from_db();self.assertNotIn("installation_id",event.details)
        self.assertEqual(backups.RecoveredOperationOrigin.objects.count(),0)

    def test_native_due_change_receipt_survives_later_deadline_and_remaining_balance(self):
        source=self.deadline_source(self.customer.id,self.debt.id)
        source.origin_user_id=self.owner.id;source.origin_installation_id=self.live.installation_id
        source.payload["original_request"]["installation_id"]=str(self.live.installation_id)
        raw={key:value for key,value in source.payload["original_request"].items() if key not in ("installation_id","debt_id")}
        path=f"/api/v1/shops/{self.a.id}/clients/{self.customer.id}/debts/{self.debt.id}/due-date/"
        native=self.client.post(path,raw,format="json");self.assertEqual(native.status_code,200,native.content)
        source.occurred_at=Activity.objects.get(id=source.operation_id).created_at
        source.payload["due_change"]["occurred_at"]=source.occurred_at.isoformat()
        CustomerDebt.objects.filter(id=self.debt.id).update(remaining_fcfa=0)
        later=self.client.post(path,{"operation_id":str(uuid.uuid4()),"base_version":2,
            "due_date":"2026-11-06","reason":"Deuxième report explicite"},format="json")
        self.assertEqual(later.status_code,200,later.content)
        capture=self.capture(source);preview=self.preview(capture);self.assertEqual(preview["outcome"],"already_known")
        response,_=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.data["business_receipt"],native.data)
        self.debt.refresh_from_db();self.assertEqual((self.debt.remaining_fcfa,self.debt.due_version),(0,3))
        self.assertEqual(self.debt.due_at,datetime(2026,11,5,23,tzinfo=timezone.utc))
        self.assertEqual(CashMovement.objects.count(),0)

    def test_recovered_revisions_replayed_after_new_generation_keep_later_native_edits(self):
        sources=(self.contact_source(self.before()),self.deadline_source(self.customer.id,self.debt.id))
        receipts=[]
        for source in sources:
            response,_=self.apply(self.capture(source));self.assertEqual(response.status_code,201,response.content);receipts.append(response.data)
        self.customer.refresh_from_db()
        later=self.client.patch(f"/api/v1/shops/{self.a.id}/clients/{self.customer.id}/",{
            "operation_id":str(uuid.uuid4()),"base_version":2,"phone":None,"reason":"Téléphone retiré explicitement"},format="json")
        self.assertEqual(later.status_code,200,later.content)
        self.current.current_slot=None;self.current.save();self.current=self.generation();self.registry=self.record(self.current)
        self.review(self.a,self.owner,"owner",confirmed=True)
        for source,receipt in zip(sources,receipts,strict=True):
            with self.subTest(operation_type=source.operation_type):
                capture=self.capture(source);preview=self.preview(capture);self.assertEqual(preview["outcome"],"already_known")
                response,command=self.apply(capture,preview);self.assertEqual(response.status_code,201,response.content)
                self.assertEqual(response.data["business_receipt"],receipt["business_receipt"])
                retry=self.client.post(self.url(f"operations/{capture.id}/integrate/"),command,format="json")
                self.assertEqual((retry.status_code,retry.data),(200,response.data))
        self.customer.refresh_from_db();self.assertIsNone(self.customer.phone);self.assertEqual(self.customer.version,3)
        self.assertEqual((backups.RecoveredOperationOrigin.objects.count(),CustomerLedgerEntry.objects.count(),CashMovement.objects.count()),(2,1,0))

    def test_foreign_targets_sources_and_withdrawn_current_roles_cannot_apply_changes(self):
        source=self.contact_source(self.before());capture=self.capture(source)
        seller=User.objects.create_user(username="cannot-recover-revision")
        member=Membership.objects.create(shop=self.a,user=seller)
        self.review(self.a,seller,"seller",membership=member,confirmed=True)
        api=self.authenticate(seller)
        self.assertEqual(api.generic('POST',self.url(f"operations/{capture.id}/integrate/"),b'invalid',content_type='application/json').status_code,403)
        self.assertEqual(self.client.get(self.url(f"operations/{capture.id}/integration-preview/",shop=self.b)).status_code,404)
        preview=self.preview(capture)
        review=backups.RestoreAccessReview.objects.get(generation=self.current,shop=self.a,user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=1,version=2,state='refused',reason='Contrôle actuel retiré')
        self.assertEqual(self.apply(capture,preview)[0].status_code,404)
        self.customer.refresh_from_db();self.assertEqual(self.customer.version,1)

    def test_previews_receipts_and_localized_invalid_snapshots_match_openapi(self):
        import json
        from drf_spectacular.generators import SchemaGenerator
        from jsonschema import Draft4Validator,FormatChecker
        def nullable(value):
            if isinstance(value,list):return [nullable(item) for item in value]
            if not isinstance(value,dict):return value
            result={key:nullable(item) for key,item in value.items() if key!='nullable'}
            return {'anyOf':[result,{'type':'null'}]} if value.get('nullable') else result
        schema=SchemaGenerator().get_schema(request=None,public=True)
        def check(response,ending,method='get'):
            path=next(path for path in schema['paths'] if path.endswith(ending))
            rule=schema['paths'][path][method]['responses'][str(response.status_code)]['content']['application/json']['schema']
            errors=list(Draft4Validator(nullable({**rule,'components':schema['components']}),format_checker=FormatChecker()).iter_errors(json.loads(response.content)))
            self.assertEqual([(list(error.absolute_path),error.validator) for error in errors],[])
        for source in (self.contact_source(self.before()),self.deadline_source(self.customer.id,self.debt.id)):
            with self.subTest(operation_type=source.operation_type):
                capture=self.capture(source);preview=self.client.get(self.url(f"operations/{capture.id}/integration-preview/"))
                self.assertEqual(preview.status_code,200,preview.content);check(preview,'/integration-preview/')
                response,_=self.apply(capture,preview.data);self.assertEqual(response.status_code,201,response.content);check(response,'/integrate/','post')
        source=self.deadline_source(self.customer.id,self.debt.id);source.payload['due_change']['after_version']=4
        capture=self.capture(source)
        for language,message in (('fr',"Les données figées de la révision client récupérée sont incohérentes."),('en','Recovery customer revision snapshot is inconsistent.')):
            with self.subTest(language=language):
                response=self.client.get(self.url(f"operations/{capture.id}/integration-preview/"),HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual((response.status_code,str(response.data['detail'])),(400,message))

    def test_foreign_entity_is_not_exposed_and_duplicate_contact_hint_invalidates_review(self):
        foreign=Customer.objects.create(shop=self.b,name="Identité confidentielle de B")
        source=self.contact_source({**self.before(),"id":str(foreign.id)})
        capture=self.capture(source);preview=self.preview(capture)
        self.assertFalse(preview["can_apply"]);self.assertIsNone(preview["current_state"])
        self.assertNotIn(foreign.name,str(preview));self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        source=self.contact_source(self.before());capture=self.capture(source);old=self.preview(capture)
        Customer.objects.create(shop=self.a,name="Nom corrigé")
        self.assertEqual(self.apply(capture,old)[0].status_code,409)
        fresh=self.preview(capture);self.assertTrue(fresh["has_possible_duplicate"])
        self.assertTrue(fresh["can_apply"])
        response,_=self.apply(capture,fresh);self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(Customer.objects.filter(shop=self.a,name="Nom corrigé").count(),2)
        foreign.refresh_from_db();self.assertEqual((foreign.name,foreign.version),("Identité confidentielle de B",1))

    def test_source_installation_of_another_actor_blocks_both_revision_kinds(self):
        for source in (self.contact_source(self.before()),self.deadline_source(self.customer.id,self.debt.id)):
            with self.subTest(operation_type=source.operation_type):
                source.origin_installation_id=self.live.installation_id
                if source.operation_type=="customer_due_change":
                    source.payload["original_request"]["installation_id"]=str(self.live.installation_id)
                capture=self.capture(source);preview=self.preview(capture)
                self.assertFalse(preview["can_apply"])
                self.assertIn("origin_identity_conflict",{row["code"] for row in preview["issues"]})
                self.assertEqual(self.apply(capture,preview)[0].status_code,409)
        self.customer.refresh_from_db();self.debt.refresh_from_db()
        self.assertEqual((self.customer.version,self.debt.due_version,CustomerLedgerEntry.objects.count()),(1,1,0))

    def test_explicit_phone_and_deadline_removal_preserves_names_and_money(self):
        source=self.contact_source(self.before())
        source.payload["original_request"].pop("name")
        source.payload["original_request"]["phone"]=None
        source.payload["customer_update"]["after"].update(name=self.customer.name,phone=None)
        response,_=self.apply(self.capture(source));self.assertEqual(response.status_code,201,response.content)
        source=self.deadline_source(self.customer.id,self.debt.id)
        source.payload["original_request"]["due_date"]=None
        source.payload["due_change"]["after_due_at"]=None
        response,_=self.apply(self.capture(source));self.assertEqual(response.status_code,201,response.content)
        self.customer.refresh_from_db();self.debt.refresh_from_db()
        self.assertEqual((self.customer.name,self.customer.phone,self.customer.version),("Nom initial",None,2))
        self.assertEqual((self.debt.due_at,self.debt.due_version,self.debt.amount_fcfa,self.debt.remaining_fcfa),(None,2,8000,5000))
        self.assertEqual(CashMovement.objects.count(),0)

    def test_adding_first_deadline_is_recognized_after_later_changes_without_a_second_revision(self):
        CustomerDebt.objects.filter(id=self.debt.id).update(due_at=None)
        source=self.deadline_source(self.customer.id,self.debt.id);source.payload["due_change"]["before_due_at"]=None
        capture=self.capture(source);response,_=self.apply(capture);self.assertEqual(response.status_code,201,response.content)
        preview=self.preview(capture);self.assertEqual(preview["outcome"],"already_known")
        again,_=self.apply(capture,preview);self.assertEqual((again.status_code,again.data["business_receipt"]),(201,response.data["business_receipt"]))
        self.debt.refresh_from_db();self.assertEqual(self.debt.due_version,2)
        self.assertEqual(CustomerLedgerEntry.objects.count(),1)
