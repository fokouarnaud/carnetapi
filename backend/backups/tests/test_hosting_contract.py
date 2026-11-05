"""Validate actual hosting HTTP bodies against the generated OpenAPI schemas."""
from datetime import datetime, timezone as utc
import json
import uuid
from unittest.mock import patch
from django.test import TestCase, override_settings
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, FormatChecker
from rest_framework.test import APIClient
from backups.models import HostingReminder
from backups.tests import test_hosting as fixtures
from backups.services.restore_gate import RestoreGateError


def nullable(value):
    if isinstance(value,list):return [nullable(item) for item in value]
    if not isinstance(value,dict):return value
    result={key:nullable(item) for key,item in value.items() if key!="nullable"}
    return {"anyOf":[result,{"type":"null"}]} if value.get("nullable") else result


class HostingContractTests(TestCase):
    setUp=fixtures.HostingApiTests.setUp
    configure=fixtures.HostingApiTests.configure
    trigger=fixtures.HostingApiTests.trigger

    def check(self,path,method,response,status):
        self.assertEqual(response.status_code,status,response.content)
        operation=self.schema["paths"]["/api/v1/hosting/"+path][method]
        self.assertTrue(operation.get("description"))
        rule=nullable({**operation["responses"][str(status)]["content"]["application/json"]["schema"],
            "components":self.schema["components"]})
        errors=list(Draft4Validator(rule,format_checker=FormatChecker()).iter_errors(json.loads(response.content)))
        self.assertEqual([(list(error.absolute_path),error.message) for error in errors],[])

    def test_new_paths_cover_nullable_config_reports_lists_trigger_retry_and_denials(self):
        self.schema=SchemaGenerator().get_schema(request=None,public=True)
        self.assertEqual(len([path for path in self.schema["paths"] if path.startswith("/api/v1/hosting/")]),7)
        self.check("configuration/","get",self.client.get("/api/v1/hosting/configuration/"),200)
        self.check("status/","get",self.client.get("/api/v1/hosting/status/"),200)
        with patch("django.utils.timezone.now",return_value=datetime(2026,11,1,tzinfo=utc.utc)):
            response,_=self.configure()
        self.check("configuration/","patch",response,200)
        with patch("django.core.mail.message.EmailMessage.send",side_effect=TimeoutError()):
            response=self.trigger(datetime(2026,11,3,tzinfo=utc.utc))
        self.check("trigger/","post",response,200)
        self.check("reminders/","get",self.client.get("/api/v1/hosting/reminders/?page_size=1"),200)
        reminder=HostingReminder.objects.get(lead_days=7)
        attempts=f"/api/v1/hosting/reminders/{reminder.id}/attempts/"
        self.check("reminders/{id}/attempts/","get",self.client.get(attempts),200)
        retry=f"/api/v1/hosting/reminders/{reminder.id}/retry/"
        data={"operation_id":str(uuid.uuid4()),"expected_attempt_id":str(reminder.current_attempt_id),
            "possible_duplicate_confirmed":True,"reason":"Reprise consciente du doublon possible"}
        with patch("django.utils.timezone.now",return_value=datetime(2026,11,3,tzinfo=utc.utc)):
            self.check("reminders/{id}/retry/","post",self.client.post(retry,data,format="json"),201)
            self.check("reminders/{id}/retry/","post",self.client.post(retry,data,format="json"),200)
            self.check("reminders/{id}/retry/","post",self.client.post(retry,{**data,"reason":"Modifié"},format="json"),409)
        data={"id":str(uuid.uuid4()),"operation_id":str(uuid.uuid4()),"observed_at":"2026-10-20T10:00:00Z",
            "service_state":"unknown","evidence_reference":"Observation privée","reason":"Chiffres inconnus"}
        self.check("observations/","post",self.client.post("/api/v1/hosting/observations/",data,format="json"),201)
        self.check("observations/","post",self.client.post("/api/v1/hosting/observations/",data,format="json"),200)
        self.check("observations/","get",self.client.get("/api/v1/hosting/observations/"),200)
        self.check("status/","get",self.client.get("/api/v1/hosting/status/"),200)
        self.check("configuration/","patch",self.client.patch("/api/v1/hosting/configuration/",{},format="json"),400)
        self.client.force_authenticate(self.owner)
        self.check("status/","get",self.client.get("/api/v1/hosting/status/"),403)
        self.check("status/","get",APIClient().get("/api/v1/hosting/status/"),401)

    def test_worker_unavailability_is_documented_for_retry_as_well_as_trigger(self):
        self.schema=SchemaGenerator().get_schema(request=None,public=True)
        with patch("backups.services.hosting.normal_database_work",side_effect=RestoreGateError("private gate failure")):
            response=self.client.post(f"/api/v1/hosting/reminders/{uuid.uuid4()}/retry/",{},format="json")
        self.check("reminders/{id}/retry/","post",response,503)
        with override_settings(HOSTING_REMINDER_TOKEN="short"):
            response=self.trigger(datetime(2026,11,3,tzinfo=utc.utc))
        self.check("trigger/","post",response,503)

    def test_global_maintenance_body_matches_each_hosting_503_without_oneof_ambiguity(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from django.test import RequestFactory
        from backups.middleware import RestoreGateMiddleware
        self.schema=SchemaGenerator().get_schema(request=None,public=True)
        with TemporaryDirectory(prefix="hosting-contract-gate-") as directory,override_settings(RESTORE_GATE_PATH=Path(directory)/"missing.sqlite3"):
            response=RestoreGateMiddleware(lambda request:self.fail("handler reached"))(RequestFactory().post("/api/v1/hosting/trigger/"))
        for path,operations in self.schema["paths"].items():
            if not path.startswith("/api/v1/hosting/"):continue
            for method in operations:
                if method in {"get","patch","post"}:
                    with self.subTest(path=path,method=method):self.check(path.removeprefix("/api/v1/hosting/"),method,response,503)
