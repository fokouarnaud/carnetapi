"""A current responsible inspects only current recovery observations in their shop."""
from datetime import timedelta
import json
import uuid
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.utils import timezone
from backups import models as backups
from backups.access_models import PERMISSION_FIELDS
from backups.tests import test_recovery_session as fixtures
from identity.models import User,Membership


class RecoveryDeviceApiTests(TransactionTestCase):
    authenticate=fixtures.RecoverySessionTests.authenticate
    generation=fixtures.RecoverySessionTests.generation
    record=fixtures.RecoverySessionTests.record
    device=fixtures.RecoverySessionTests.device

    def setUp(self):
        fixtures.RecoverySessionTests.setUp(self)
        historical=self.generation()
        old=self.record(historical,inventory_recorded_at=timezone.now())
        self.old=self.device(old,self.a,label="Historical phone")
        historical.current_slot=None
        historical.save()
        self.current=self.generation()
        self.registry=self.record(self.current,inventory_recorded_at=timezone.now())
        self.a_devices=[self.device(self.registry,self.a,label=f"A phone {i}") for i in range(3)]
        self.b_device=self.device(self.registry,self.b,label="B private phone")
        self.review(self.a,self.owner,"owner",confirmed=True)
        self.review(self.b,self.owner,"owner")
        self.client=self.authenticate(self.owner)

    def review(self,shop,user,role,*,membership=None,confirmed=False):
        review=backups.RestoreAccessReview.objects.create(generation=self.current,shop=shop,user=user,
            archived_role=role,membership=membership,was_active=True,archived_permissions=dict.fromkeys(PERMISSION_FIELDS,True))
        if confirmed:
            backups.RestoreAccessDecision.objects.create(review=review,operation_id=uuid.uuid4(),requested_by=self.technical,
                previous_version=0,version=1,state="confirmed",reason="Droits actuels vérifiés")
        return review

    def url(self,pk=None,shop=None):
        return f"/api/v1/shops/{(shop or self.a).id}/recovery/devices/"+(f"{pk}/" if pk else "")

    def test_current_scope_is_paginated_without_history_foreign_phones_or_double_effect(self):
        first=self.client.get(self.url(),{"page_size":2})
        self.assertEqual(first.status_code,200)
        self.assertEqual(first.data["count"],3)
        self.assertEqual(first.data["restoration"]["known_devices"],3)
        self.assertEqual(first.data["restoration"]["state"],"recovering")
        second=self.client.get(first.data["next"])
        ids=[row["id"] for row in first.data["results"]+second.data["results"]]
        self.assertEqual(set(ids),{str(row.id) for row in self.a_devices})
        self.assertEqual(len(ids),3)
        self.assertNotIn("B private phone",json.dumps(json.loads(first.content)))
        own=self.client.get(self.url(self.a_devices[0].id))
        self.assertEqual(own.status_code,200)
        self.assertEqual(own.data["origin_installation_id"],str(self.a_devices[0].origin_installation_id))
        self.assertEqual(own.data["restoration"]["known_devices"],3)
        for pk in (self.b_device.id,self.old.id,uuid.uuid4()):
            with self.subTest(pk=pk):
                self.assertEqual(self.client.get(self.url(pk)).status_code,404)
        self.assertEqual(self.client.get(self.url(shop=self.b)).status_code,404)
        self.assertEqual(backups.RecoveryDevice.objects.count(),5)

    def test_sellers_and_technical_accounts_have_no_implicit_business_inspection_access(self):
        seller=User.objects.create_user(username="recovery-list-seller")
        member=Membership.objects.create(user=seller,shop=self.a)
        self.review(self.a,seller,"seller",membership=member,confirmed=True)
        seller_api=self.authenticate(seller)
        self.assertEqual(seller_api.get(self.url()).status_code,403)
        technical=self.authenticate(self.technical)
        self.assertEqual(technical.get(self.url()).status_code,404)
        self.assertEqual(self.client.post(self.url(),{"origin_user_id":str(self.owner.id)},format="json").status_code,405)
        for method in ("patch","delete"):
            self.assertEqual(getattr(self.client,method)(self.url(self.a_devices[0].id),{},format="json").status_code,405)
        self.assertEqual(backups.RecoveryDevice.objects.count(),5)

    def test_current_manager_access_is_rechecked_with_the_same_signed_session(self):
        manager=User.objects.create_user(username="recovery-list-manager")
        member=Membership.objects.create(user=manager,shop=self.a,role="manager")
        self.review(self.a,manager,"manager",membership=member,confirmed=True)
        client=self.authenticate(manager)
        self.assertEqual(client.get(self.url()).status_code,200)
        Membership.objects.filter(id=member.id).update(is_active=False)
        self.assertEqual(client.get(self.url()).status_code,404)

    def test_absent_inventory_never_turns_an_empty_list_into_proof_of_recovery(self):
        backups.RecoveryDevice.objects.filter(session=self.registry).delete()
        self.registry.delete()
        response=self.client.get(self.url())
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data["count"],0)
        self.assertIsNone(response.data["restoration"]["known_devices"])
        self.assertEqual(response.data["restoration"]["state"],"recovering")
        self.assertEqual(self.client.get(self.url(self.old.id)).status_code,404)

    def test_anonymous_and_expired_sessions_are_denied_without_exposing_observations(self):
        from rest_framework.test import APIClient
        from identity.models import DeviceSession
        self.assertEqual(APIClient().get(self.url()).status_code,401)
        DeviceSession.objects.filter(installation__user=self.owner).update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.client.get(self.url()).status_code,401)
        self.assertEqual(backups.RecoveryDevice.objects.count(),5)

    def test_denial_describes_inspection_in_the_requested_language(self):
        seller=User.objects.create_user(username="recovery-list-translated-seller")
        member=Membership.objects.create(user=seller,shop=self.a)
        self.review(self.a,seller,"seller",membership=member,confirmed=True)
        client=self.authenticate(seller)
        for language,message in (("en","Only a current shop responsible can inspect recovery devices."),
                ("fr","Seul un responsable actuel de la boutique peut consulter cet inventaire d’appareils.")):
            with self.subTest(language=language):
                response=client.get(self.url(),HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(response.status_code,403)
                self.assertEqual(response.data["code"],"permission_denied")
                self.assertEqual(str(response.data["detail"]),message)

    def test_openapi_matches_observation_pages_details_and_access_errors(self):
        from drf_spectacular.generators import SchemaGenerator
        from jsonschema import Draft4Validator,FormatChecker
        def nullable(value):
            if isinstance(value,list):return [nullable(item) for item in value]
            if not isinstance(value,dict):return value
            result={key:nullable(item) for key,item in value.items() if key!="nullable"}
            return {"anyOf":[result,{"type":"null"}]} if value.get("nullable") else result
        schema=SchemaGenerator().get_schema(request=None,public=True)
        paths={key:next(path for path in schema["paths"] if path.endswith(ending))
            for key,ending in (("list","/recovery/devices/"),("detail","/recovery/devices/{id}/"))}
        def check(response,key):
            document=schema["paths"][paths[key]]["get"]["responses"][str(response.status_code)]
            rule=nullable({**document["content"]["application/json"]["schema"],"components":schema["components"]})
            errors=list(Draft4Validator(rule,format_checker=FormatChecker()).iter_errors(json.loads(response.content)))
            self.assertEqual([(list(error.absolute_path),error.validator) for error in errors],[])
        check(self.client.get(self.url()),"list")
        check(self.client.get(self.url(self.a_devices[0].id)),"detail")
        check(self.client.get(self.url(self.b_device.id)),"detail")
        check(self.client.get(self.url(shop=self.b)),"list")
        seller=User.objects.create_user(username="recovery-schema-seller")
        member=Membership.objects.create(user=seller,shop=self.a)
        self.review(self.a,seller,"seller",membership=member,confirmed=True)
        check(self.authenticate(seller).get(self.url()),"list")
        from rest_framework.test import APIClient
        check(APIClient().get(self.url()),"list")

    def test_large_inventory_is_bounded_in_sql_without_queries_per_observation(self):
        with CaptureQueriesContext(connection) as first_queries:
            small=self.client.get(self.url())
        self.assertEqual(small.status_code,200)
        backups.RecoveryDevice.objects.bulk_create([backups.RecoveryDevice(session=self.registry,shop=self.a,
            origin_installation_id=uuid.uuid4(),origin_user_id=self.owner.id,label=f"Phone {i}") for i in range(101)])
        with CaptureQueriesContext(connection) as large_queries:
            large=self.client.get(self.url(),{"page_size":100000})
        self.assertEqual(large.status_code,200)
        self.assertEqual(large.data["count"],104)
        self.assertEqual(large.data["restoration"]["known_devices"],104)
        self.assertEqual(len(large.data["results"]),100)
        self.assertIsNotNone(large.data["next"])
        self.assertEqual(len(large_queries),len(first_queries))
