"""Current responsible deposits copies, without replaying business effects."""
from copy import deepcopy
import json
import uuid

from django.db import connection,IntegrityError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from backups import models as backups
from backups.tests import test_recovery_devices_api as fixtures
from cash.models import CashMovement
from common.models import Activity
from identity.models import Membership,User
from sales.models import Sale
from stock.models import StockMovement


class RecoveryJournalApiTests(TestCase):
    setUp=fixtures.RecoveryDeviceApiTests.setUp
    authenticate=fixtures.RecoveryDeviceApiTests.authenticate
    generation=fixtures.RecoveryDeviceApiTests.generation
    record=fixtures.RecoveryDeviceApiTests.record
    device=fixtures.RecoveryDeviceApiTests.device
    review=fixtures.RecoveryDeviceApiTests.review

    def url(self,suffix="journals/",shop=None):
        return f"/api/v1/shops/{(shop or self.a).id}/recovery/"+suffix

    def payload(self):
        sale,collection=uuid.uuid4(),uuid.uuid4()
        return {"operation_id":str(uuid.uuid4()),"session_id":str(self.registry.id),"shop_id":str(self.a.id),"format_version":1,
            "reason":"Copie remise par le vendeur retiré", "operations":[
                {"operation_id":str(sale),"origin_installation_id":str(self.a_devices[0].origin_installation_id),
                 "origin_user_id":str(self.a_devices[0].origin_user_id),"operation_type":"sale",
                 "occurred_at":"2026-10-22T09:00:00Z","origin_recorded_at":"2026-10-22T09:01:00Z",
                 "payload":{"amount_fcfa":5000,"quantity_milli":1000},"dependencies":[]},
                {"operation_id":str(collection),"origin_installation_id":str(self.a_devices[0].origin_installation_id),
                 "origin_user_id":str(self.a_devices[0].origin_user_id),"operation_type":"customer_collection",
                 "occurred_at":"2026-10-22T09:05:00Z","origin_recorded_at":"2026-10-22T09:05:00Z",
                 "payload":{"amount_fcfa":3000},"dependencies":[str(sale)]}]}

    def post(self,data):
        return self.client.post(self.url(),data,format="json")

    def test_deposit_is_durable_scoped_and_inspectable_without_rights_or_business_effects(self):
        data=self.payload()
        before=[model.objects.count() for model in (Activity,CashMovement,StockMovement,Sale,User)]
        response=self.post(data)
        self.assertEqual(response.status_code,201,response.content)
        self.assertEqual(response.data["capture_state"],"captured")
        self.assertEqual(response.data["operation_count"],2)
        self.assertEqual(response.data["new_operation_count"],2)
        self.assertEqual(response.data["business_effects_created"],0)
        self.assertEqual(response.data["submitted_by"],str(self.owner.id))
        self.assertEqual(response.data["reason"],data["reason"])
        receipt=self.client.get(self.url(f"journals/{response.data['id']}/"))
        self.assertEqual(receipt.status_code,200,receipt.content)
        self.assertEqual(receipt.data,response.data)
        page=self.client.get(self.url("operations/"),{"page_size":1})
        self.assertEqual(page.status_code,200,page.content)
        self.assertEqual(page.data["count"],2)
        self.assertEqual(page.data["restoration"]["state"],"recovering")
        first=page.data["results"][0]
        by_id={row["operation_id"]:row for row in data["operations"]}
        self.assertEqual(first["origin_user_id"],data["operations"][0]["origin_user_id"])
        self.assertEqual(first["payload"],by_id[first["operation_id"]]["payload"])
        detail=self.client.get(self.url(f"operations/{first['id']}/"))
        self.assertEqual(detail.status_code,200,detail.content)
        self.assertEqual(detail.data["payload"],first["payload"])
        second=self.client.get(page.data["next"])
        self.assertEqual(second.status_code,200,second.content)
        self.assertNotEqual(first["id"],second.data["results"][0]["id"])
        following=second.data["results"][0]
        self.assertEqual(following["dependencies"],by_id[following["operation_id"]]["dependencies"])
        self.assertEqual([model.objects.count() for model in (Activity,CashMovement,StockMovement,Sale,User)],before)

    def test_same_command_and_other_deposits_recognize_the_same_source_operations(self):
        data=self.payload()
        original=self.post(data)
        self.assertEqual(original.status_code,201,original.content)
        replay=deepcopy(data)
        replay["operations"][0]["occurred_at"]="2026-10-22T10:00:00+01:00"
        replay["operations"][0]["payload"]={"quantity_milli":1000,"amount_fcfa":5000}
        response=self.post(replay)
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(response.data,original.data)
        again=self.post({**replay,"operation_id":str(uuid.uuid4()),"reason":"Autre copie survivante"})
        self.assertEqual(again.status_code,201,again.content)
        self.assertEqual(again.data["new_operation_count"],0)
        self.assertEqual(again.data["operations"],original.data["operations"])
        self.assertEqual(backups.RecoveryJournalDeposit.objects.count(),2)
        self.assertEqual(backups.RecoveryJournalOperation.objects.count(),2)

    def test_conflicting_uuid_does_not_overwrite_and_the_whole_new_deposit_rolls_back(self):
        data=self.payload()
        original=self.post(data)
        self.assertEqual(original.status_code,201,original.content)
        changed=deepcopy(data)
        changed["operations"][0]["payload"]["amount_fcfa"]=9999
        self.assertEqual(self.post(changed).status_code,409)
        changed["operation_id"]=str(uuid.uuid4())
        changed["operations"].insert(0,{**deepcopy(data["operations"][0]),"operation_id":str(uuid.uuid4())})
        self.assertEqual(self.post(changed).status_code,409)
        self.assertEqual(backups.RecoveryJournalDeposit.objects.count(),1)
        self.assertEqual(backups.RecoveryJournalOperation.objects.count(),2)
        self.assertEqual(self.client.get(self.url(f"journals/{original.data['id']}/")).data,original.data)

    def test_authorization_precedes_parsing_and_removed_access_cannot_deposit_or_inspect(self):
        seller=User.objects.create_user(username="journal-current-seller")
        member=Membership.objects.create(user=seller,shop=self.a)
        self.review(self.a,seller,"seller",membership=member,confirmed=True)
        for client,shop,status in ((self.authenticate(seller),self.a,403),(self.client,self.b,404),
                                   (self.authenticate(self.technical),self.a,404)):
            with self.subTest(status=status,shop=shop.id):
                response=client.generic("POST",self.url(shop=shop),b"not-json",content_type="application/json")
                self.assertEqual(response.status_code,status,response.content)
        manager=User.objects.create_user(username="journal-current-manager")
        manager_member=Membership.objects.create(user=manager,shop=self.a,role="manager")
        self.review(self.a,manager,"manager",membership=manager_member,confirmed=True)
        client=self.authenticate(manager)
        response=client.post(self.url(),self.payload(),format="json")
        self.assertEqual(response.status_code,201,response.content)
        manager_member.is_active=False
        manager_member.save(update_fields=["is_active"])
        for method,suffix in (("POST","journals/"),("GET","journals/"),("GET",f"journals/{response.data['id']}/"),("GET","operations/")):
            denied=client.generic(method,self.url(suffix),b"not-json",content_type="application/json")
            self.assertEqual(denied.status_code,404,denied.content)

    def test_foreign_shop_and_historical_deposits_are_not_visible(self):
        response=self.post(self.payload())
        self.assertEqual(response.status_code,201,response.content)
        review=backups.RestoreAccessReview.objects.get(generation=self.current,shop=self.b,user=self.owner)
        backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
            previous_version=0,version=1,state="confirmed",reason="Contrôle actuel de B")
        for suffix in (f"journals/{response.data['id']}/",f"operations/{response.data['operations'][0]['capture_id']}/"):
            denied=self.client.get(self.url(suffix,shop=self.b))
            self.assertEqual(denied.status_code,404,denied.content)
        self.assertEqual(self.client.get(self.url(shop=self.b)).data["count"],0)
        self.current.current_slot=None
        self.current.save(update_fields=["current_slot"])
        self.current=self.generation()
        self.registry=self.record(self.current)
        self.review(self.a,self.owner,"owner",confirmed=True)
        self.assertEqual(self.client.get(self.url()).data["count"],0)
        self.assertEqual(self.client.get(self.url(f"journals/{response.data['id']}/")).status_code,404)

    def test_unknown_dependencies_are_captured_without_inventing_referenced_operations(self):
        data=self.payload()
        missing=str(uuid.uuid4())
        data["operations"][0]["dependencies"]=[missing]
        response=self.post(data)
        self.assertEqual(response.status_code,201,response.content)
        row=backups.RecoveryJournalOperation.objects.get(operation_id=data["operations"][0]["operation_id"])
        self.assertEqual(row.dependencies,[missing])
        self.assertFalse(backups.RecoveryJournalOperation.objects.filter(operation_id=missing).exists())

    def test_stale_generation_and_invalid_version_or_shapes_are_refused_without_capture(self):
        data=self.payload()
        for version in (0,2,True,1.5,"1"):
            with self.subTest(version=version):self.assertEqual(self.post({**data,"format_version":version}).status_code,400)
        for changes in ({"reason":" "},{"operations":[]},{"session_id":str(uuid.uuid4())}):
            response=self.post({**data,**changes})
            self.assertEqual(response.status_code,409 if "session_id" in changes else 400,response.content)
        duplicate=deepcopy(data)
        duplicate["operations"].append(duplicate["operations"][0])
        self.assertEqual(self.post(duplicate).status_code,400)
        self.assertEqual(backups.RecoveryJournalDeposit.objects.count(),0)

    def test_bounded_json_rejects_duplicate_keys_oversize_and_credentials_without_persisting_them(self):
        data=self.payload()
        raw=json.dumps(data).replace('"amount_fcfa": 5000','"amount_fcfa":5000,"amount_fcfa":9000',1).encode()
        self.assertEqual(self.client.generic("POST",self.url(),raw,content_type="application/json").status_code,400)
        for change in ({"note":"é"*500001},{"refresh_token":"private-do-not-store"},{"nested":{"pin":"123456"}}):
            changed=deepcopy(data)
            changed["operations"][0]["payload"]=change
            response=self.post(changed)
            self.assertEqual(response.status_code,400,response.content)
            self.assertNotIn(b"private-do-not-store",response.content)
            self.assertNotIn(b"123456",response.content)
        self.assertEqual(self.client.generic("POST",self.url(),b" "*2000001,content_type="application/json").status_code,400)
        self.assertEqual(backups.RecoveryJournalDeposit.objects.count(),0)

    def test_permission_denial_is_explicit_in_french_and_english(self):
        seller=User.objects.create_user(username="journal-language-seller")
        member=Membership.objects.create(user=seller,shop=self.a)
        self.review(self.a,seller,"seller",membership=member,confirmed=True)
        client=self.authenticate(seller)
        for language,text in (("fr","Seul un responsable actuel de la boutique peut déposer ou consulter les journaux de récupération."),
                              ("en","Only a current shop responsible can deposit or inspect recovery journals.")):
            with self.subTest(language=language):
                response=client.get(self.url(),HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(response.status_code,403,response.content)
                self.assertEqual(str(response.data["detail"]),text)

    def test_operation_batches_and_read_pages_are_bounded_without_queries_per_capture(self):
        base=self.payload()
        template=base["operations"][0]
        entries=[{**template,"operation_id":str(uuid.uuid4())} for _ in range(101)]
        too_many=self.post({**base,"operations":entries[:51]})
        self.assertEqual(too_many.status_code,400,too_many.content)
        self.assertEqual(backups.RecoveryJournalDeposit.objects.count(),0)
        first=self.post({**base,"operations":entries[:1]})
        self.assertEqual(first.status_code,201,first.content)
        with CaptureQueriesContext(connection) as small_queries:
            small=self.client.get(self.url("operations/"))
        self.assertEqual(small.status_code,200,small.content)
        for chunk in (entries[1:51],entries[51:]):
            response=self.post({**base,"operation_id":str(uuid.uuid4()),"operations":chunk})
            self.assertEqual(response.status_code,201,response.content)
        with CaptureQueriesContext(connection) as large_queries:
            large=self.client.get(self.url("operations/"),{"page_size":100000})
        self.assertEqual(large.status_code,200,large.content)
        self.assertEqual(large.data["count"],101)
        self.assertEqual(len(large.data["results"]),100)
        self.assertEqual(len(large_queries),len(small_queries))
        last=self.client.get(large.data["next"])
        self.assertEqual(len(last.data["results"]),1)
        self.assertNotIn(last.data["results"][0]["id"],{row["id"] for row in large.data["results"]})

    def test_real_receipts_pages_and_errors_match_the_openapi_schema(self):
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
            document=schema["paths"][path][method]["responses"][str(response.status_code)]
            rule=nullable({**document["content"]["application/json"]["schema"],"components":schema["components"]})
            errors=list(Draft4Validator(rule,format_checker=FormatChecker()).iter_errors(json.loads(response.content)))
            self.assertEqual([(list(error.absolute_path),error.validator) for error in errors],[])
        data=self.payload()
        first=self.post(data)
        self.assertEqual(first.status_code,201,first.content)
        check(first,"/recovery/journals/","post")
        check(self.post(data),"/recovery/journals/","post")
        check(self.post({**data,"reason":"Changed"}),"/recovery/journals/","post")
        check(self.post({**data,"format_version":2}),"/recovery/journals/","post")
        for suffix,ending in (("journals/","/recovery/journals/"),("operations/","/recovery/operations/"),
                (f"journals/{first.data['id']}/","/recovery/journals/{id}/"),
                (f"operations/{first.data['operations'][0]['capture_id']}/","/recovery/operations/{id}/")):
            with self.subTest(route=suffix):check(self.client.get(self.url(suffix)),ending)
        check(self.client.get(self.url(f"journals/{uuid.uuid4()}/")),"/recovery/journals/{id}/")

    def test_invalid_unicode_is_refused_as_input_without_an_internal_error_or_capture(self):
        data=self.payload()
        invalid="\ud800"
        for field in ("reason","operation_type","unknown_key"):
            changed=deepcopy(data)
            if field=="reason":changed["reason"]=invalid
            elif field=="operation_type":changed["operations"][0]["operation_type"]=invalid
            else:changed[invalid]="unexpected"
            with self.subTest(field=field):
                response=self.client.generic("POST",self.url(),json.dumps(changed).encode(),content_type="application/json")
                self.assertEqual(response.status_code,400,response.content)
        self.assertEqual(backups.RecoveryJournalDeposit.objects.count(),0)

    def test_database_failure_before_receipt_commit_leaves_no_partial_deposit_and_retry_succeeds(self):
        failures=(
            "CREATE TEMP TRIGGER reject_recovery_write BEFORE INSERT ON backups_recoveryjournaloperation "
            "WHEN NEW.operation_type='customer_collection' BEGIN SELECT RAISE(ABORT,'test_capture_failure'); END",
            "CREATE TEMP TRIGGER reject_recovery_write BEFORE UPDATE OF receipt ON backups_recoveryjournaldeposit "
            "BEGIN SELECT RAISE(ABORT,'test_receipt_failure'); END")
        for statement in failures:
            data=self.payload()
            before=(backups.RecoveryJournalDeposit.objects.count(),backups.RecoveryJournalOperation.objects.count())
            with self.subTest(failure=statement.split(' ON ')[-1]):
                with connection.cursor() as cursor:cursor.execute(statement)
                try:
                    with self.assertRaises(IntegrityError):self.post(data)
                    self.assertEqual((backups.RecoveryJournalDeposit.objects.count(),backups.RecoveryJournalOperation.objects.count()),before)
                finally:
                    with connection.cursor() as cursor:cursor.execute("DROP TRIGGER IF EXISTS reject_recovery_write")
                retried=self.post(data)
                self.assertEqual(retried.status_code,201,retried.content)
                self.assertEqual(retried.data["operation_count"],2)

    def test_capture_preserves_a_500_line_sale_with_the_full_allowed_unicode_labels(self):
        data=self.payload()
        snapshot={"id":str(uuid.uuid4()),"mode":"cash","customer_id":None,"due_at":None,
            "reference_total_fcfa":250000,"discount_total_fcfa":0,"net_total_fcfa":250000,
            "lines":[{"id":str(uuid.uuid4()),"product_id":str(uuid.uuid4()),"product_version":1,"pack_id":None,
                "product_name":"🛒"*150,"unit_kind":"piece","unit_label":"📦"*80,
                "quantity_milli":1000,"factor_milli":1000,"base_quantity_milli":1000,"unit_price_fcfa":500,
                "reference_fcfa":500,"own_discount_fcfa":0,"global_discount_fcfa":0,"net_fcfa":500} for _ in range(500)],
            "payments":[{"id":str(uuid.uuid4()),"method":"cash","amount_fcfa":250000,
                "other_name":"","external_reference":"","received_fcfa":250000,"returned_fcfa":0}]}
        payload={"schema_version":1,"sale":snapshot}
        encoded=json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode()
        self.assertGreater(len(encoded),64000)
        self.assertLess(len(encoded),1000000)
        data["operations"]=[{**data["operations"][0],"payload":payload}]
        response=self.post(data)
        self.assertEqual(response.status_code,201,response.content)
        stored=backups.RecoveryJournalOperation.objects.get(operation_id=data["operations"][0]["operation_id"])
        self.assertEqual(stored.payload,payload)
        self.assertEqual(len(stored.payload["sale"]["lines"]),500)
