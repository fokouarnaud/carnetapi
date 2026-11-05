"""US-44: provisional ledgers explain restoration without leaking device identities."""
import json
import uuid

from django.test import TestCase
from django.utils import timezone

from backups import models as backups
from backups.access_models import PERMISSION_FIELDS
from backups.tests import test_recovery_session as fixtures
from cash.models import CashMovement
from common.models import Activity
from customers.models import Customer
from identity.models import Membership, User
from purchasing.models import Supplier


class RecoveryReportTests(TestCase):
    authenticate=fixtures.RecoverySessionTests.authenticate
    generation=fixtures.RecoverySessionTests.generation
    confirm=fixtures.RecoverySessionTests.confirm
    record=fixtures.RecoverySessionTests.record
    device=fixtures.RecoverySessionTests.device

    def setUp(self):
        fixtures.RecoverySessionTests.setUp(self)
        self.customer=Customer.objects.create(shop=self.a,name="Client A")
        self.supplier=Supplier.objects.create(shop=self.a,name="Fournisseur A")
        self.foreign_customer=Customer.objects.create(shop=self.b,name="Client B")
        self.foreign_supplier=Supplier.objects.create(shop=self.b,name="Fournisseur B")
        self.client=self.authenticate(self.owner)
        for url,extra in (
            (self.paths()["customer"].replace("statement/","debts/"),{}),
            (self.paths()["supplier"].replace("statement/","debts/"),{}),
            (self.paths()["cash"],{"kind":"opening","method":"cash"}),
            (self.paths()["cash"],{"kind":"addition","method":"orange"}),
        ):
            response=self.client.post(url,{"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),
                "amount_fcfa":1200,"occurred_at":"2026-10-01T08:00:00Z","reason":"Solde physique initial",**extra},format="json")
            self.assertEqual(response.status_code,201,response.content)
        self.cutoff=timezone.now().isoformat()

    def paths(self,shop=None):
        shop=shop or self.a
        customer=self.customer if shop==self.a else self.foreign_customer
        supplier=self.supplier if shop==self.a else self.foreign_supplier
        base=f"/api/v1/shops/{shop.id}/"
        return {"customer":base+f"clients/{customer.id}/statement/",
            "supplier":base+f"suppliers/{supplier.id}/statement/","cash":base+"cash/"}

    def restore(self):
        generation=self.generation()
        self.confirm(generation,self.a,self.owner)
        backups.RestoreAccessReview.objects.create(generation=generation,shop=self.b,user=self.owner,
            archived_role="owner",was_active=True,archived_permissions=dict.fromkeys(PERMISSION_FIELDS,True))
        return generation

    def test_no_restore_unknown_inventory_and_scoped_inventory_preserve_the_same_ledger(self):
        baseline={}
        for key,path in self.paths().items():
            response=self.client.get(path,{"as_of":self.cutoff,"page_size":1})
            self.assertEqual(response.status_code,200,response.content)
            self.assertIn("restoration",response.data)
            self.assertIsNone(response.data["restoration"])
            baseline[key]=json.loads(response.content)
            self.assertTrue(response.data["provisional"])
        self.assertEqual(baseline["customer"]["closing_balance_fcfa"],1200)
        self.assertEqual(baseline["supplier"]["closing_balance_fcfa"],1200)
        self.assertEqual(baseline["cash"]["totals_by_method_fcfa"],{"cash":1200,"orange":1200})
        generation=self.restore()
        for path in self.paths().values():
            response=self.client.get(path,{"as_of":self.cutoff})
            self.assertEqual(response.status_code,200,response.content)
            self.assertIsNone(response.data["restoration"]["known_devices"])
            self.assertIsNone(response.data["restoration"]["session_id"])
        registry=self.record(generation,snapshot_at=self.snapshot_at,inventory_recorded_at=timezone.now())
        private=self.device(registry,self.a)
        self.device(registry,self.a)
        for _ in range(3):self.device(registry,self.b)
        before=(Activity.objects.count(),CashMovement.objects.count(),backups.RecoveryDevice.objects.count())
        for key,path in self.paths().items():
            with self.subTest(report=key):
                response=self.client.get(path,{"as_of":self.cutoff,"page_size":1})
                self.assertEqual(response.status_code,200,response.content)
                body=json.loads(response.content)
                profile=body.pop("restoration")
                self.assertEqual(profile["known_devices"],2)
                self.assertEqual(profile["generation_id"],str(generation.id))
                self.assertEqual(profile["state"],"recovering")
                self.assertEqual(profile["snapshot_at"],"2026-10-20T08:00:00Z")
                expected={key:value for key,value in baseline[key].items() if key!="restoration"}
                self.assertEqual(body,expected)
                for secret in (str(private.origin_installation_id),str(private.origin_user_id),"Private phone"):
                    self.assertNotIn(secret,json.dumps(profile))
        self.assertEqual(before,(Activity.objects.count(),CashMovement.objects.count(),backups.RecoveryDevice.objects.count()))
        cash=self.client.get(self.paths()["cash"],{"as_of":self.cutoff,"page_size":1})
        second=self.client.get(cash.data["next"])
        self.assertEqual(second.status_code,200,second.content)
        self.assertEqual(second.data["count"],2)
        self.assertEqual(second.data["restoration"]["known_devices"],2)
        self.assertEqual(second.data["totals_by_method_fcfa"],{"cash":1200,"orange":1200})
        self.assertNotEqual(cash.data["results"][0]["id"],second.data["results"][0]["id"])

    def test_observed_zero_is_still_recovering_and_foreign_or_unconfirmed_scopes_are_denied(self):
        generation=self.restore()
        registry=self.record(generation,inventory_recorded_at=timezone.now())
        self.device(registry,self.b)
        for key,path in self.paths().items():
            with self.subTest(report=key):
                response=self.client.get(path)
                self.assertEqual(response.status_code,200,response.content)
                self.assertIn("restoration",response.data)
                self.assertEqual(response.data["restoration"]["known_devices"],0)
                self.assertEqual(response.data["restoration"]["state"],"recovering")
                denied=self.client.get(self.paths(self.b)[key])
                self.assertEqual(denied.status_code,404,denied.content)
                self.assertNotIn("restoration",denied.data)
        for path in (self.paths()["customer"].replace(str(self.customer.id),str(self.foreign_customer.id)),
                     self.paths()["supplier"].replace(str(self.supplier.id),str(self.foreign_supplier.id))):
            self.assertEqual(self.client.get(path).status_code,404)

    def test_current_permissions_remain_required_and_personal_cash_exposes_no_global_movements(self):
        generation=self.restore()
        registry=self.record(generation,inventory_recorded_at=timezone.now())
        self.device(registry,self.a)
        seller=User.objects.create_user(username="restore-report-seller")
        membership=Membership.objects.create(shop=self.a,user=seller)
        self.confirm(generation,self.a,seller,membership=membership)
        client=self.authenticate(seller)
        for key in ("customer","supplier"):
            self.assertEqual(client.get(self.paths()[key]).status_code,403)
        personal=client.get(self.paths()["cash"])
        self.assertEqual(personal.status_code,200,personal.content)
        self.assertEqual(personal.data["scope"],"personal")
        self.assertEqual(personal.data["results"],[])
        self.assertEqual(personal.data["totals_by_method_fcfa"],{})
        self.assertIn("restoration",personal.data)
        self.assertEqual(personal.data["restoration"]["known_devices"],1)
        self.assertEqual(client.get(self.paths()["cash"],{"scope":"global"}).status_code,403)
        membership.is_active=False
        membership.save(update_fields=["is_active"])
        self.assertEqual(client.get(self.paths()["cash"]).status_code,404)
        self.assertNotIn("restoration",client.get(self.paths()["cash"]).data)

    def test_current_generation_replaces_historical_metadata_without_changing_report_cutoff(self):
        first=self.restore()
        registry=self.record(first,snapshot_at=self.snapshot_at,inventory_recorded_at=timezone.now())
        self.device(registry,self.a)
        first.current_slot=None
        first.save(update_fields=["current_slot"])
        current=self.restore()
        for path in self.paths().values():
            response=self.client.get(path,{"as_of":self.cutoff})
            self.assertEqual(response.status_code,200,response.content)
            self.assertIn("restoration",response.data)
            self.assertEqual(response.data["restoration"]["generation_id"],str(current.id))
            self.assertIsNone(response.data["restoration"]["known_devices"])
            self.assertEqual(response.data["as_of"],self.cutoff.replace("+00:00","Z"))

    def test_openapi_matches_real_report_bodies_for_absent_unknown_and_recorded_inventory(self):
        from drf_spectacular.generators import SchemaGenerator
        from jsonschema import Draft4Validator,FormatChecker
        def nullable(value):
            if isinstance(value,list):return [nullable(item) for item in value]
            if not isinstance(value,dict):return value
            result={key:nullable(item) for key,item in value.items() if key!="nullable"}
            return {"anyOf":[result,{"type":"null"}]} if value.get("nullable") else result
        schema=SchemaGenerator().get_schema(request=None,public=True)
        endings={"customer":"/clients/{client_id}/statement/","supplier":"/suppliers/{supplier_id}/statement/","cash":"/cash/"}
        for phase in ("absent","unknown","recorded"):
            if phase=="unknown":generation=self.restore()
            if phase=="recorded":
                registry=self.record(generation,inventory_recorded_at=timezone.now())
                self.device(registry,self.a)
            for key,path in self.paths().items():
                with self.subTest(phase=phase,report=key):
                    response=self.client.get(path)
                    self.assertEqual(response.status_code,200,response.content)
                    self.assertIn("restoration",response.data)
                    schema_path=next(path for path in schema["paths"] if path.endswith(endings[key]))
                    document=schema["paths"][schema_path]["get"]["responses"]["200"]
                    rule=nullable({**document["content"]["application/json"]["schema"],"components":schema["components"]})
                    errors=list(Draft4Validator(rule,format_checker=FormatChecker()).iter_errors(json.loads(response.content)))
                    self.assertEqual([(list(error.absolute_path),error.validator) for error in errors],[])
