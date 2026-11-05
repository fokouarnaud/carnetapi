"""Technical closure is reviewable through HTTP and cannot grant commercial access."""
import json
import uuid

from django.test import TestCase
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, FormatChecker
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from backups import models as backups
from backups.tests import test_recovery_closures as fixtures
from backups.tests.test_recovery_purchase_contract import nullable_schema
from common.services.errors import OperationConflict
from common.services.operations import original_receipt


class RecoveryClosureApiTests(TestCase):
    setUp = fixtures.RecoveryClosureTests.setUp
    preview = fixtures.RecoveryClosureTests.preview
    command = fixtures.RecoveryClosureTests.command
    capture = fixtures.RecoveryClosureTests.capture
    close = fixtures.RecoveryClosureTests.close

    def api(self, user=None):
        client = APIClient()
        token = AccessToken.for_user(user or self.technical)
        token["session_id"] = str(self.live.id)
        client.credentials(HTTP_AUTHORIZATION="Bearer " + str(token))
        return client

    def url(self, action):
        return f"/api/v1/recovery/sessions/{self.registry.id}/{action}/"

    def test_preview_close_retry_and_metadata_match_the_actual_openapi_contract(self):
        client = self.api()
        preview = client.get(self.url("closure-preview"))
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertTrue(preview.data["can_close_partial"])
        data = self.command(preview_token=preview.data["preview_token"])
        closed = client.post(self.url("closures"), data, format="json")
        self.assertEqual(closed.status_code, 201, closed.content)
        self.assertEqual(closed.data["state"], "closed_partial")
        replay = client.post(self.url("closures"), data, format="json")
        self.assertEqual((replay.status_code, replay.data), (200, closed.data))
        schema = SchemaGenerator().get_schema(request=None, public=True)
        for response, ending, method in ((preview, "/closure-preview/", "get"),
                                          (closed, "/closures/", "post"),
                                          (replay, "/closures/", "post")):
            path = next(path for path in schema["paths"] if path.endswith(ending))
            rule = schema["paths"][path][method]["responses"][str(response.status_code)]["content"]["application/json"]["schema"]
            validator = Draft4Validator(nullable_schema({**rule, "components": schema["components"]}),
                                        format_checker=FormatChecker())
            with self.subTest(response=response.status_code, path=ending):
                self.assertEqual([error.message for error in validator.iter_errors(json.loads(response.content))], [])

    def test_non_technical_and_revoked_session_are_denied_even_for_an_exact_retry(self):
        from identity.models import DeviceSession, User
        client = self.api()
        data = self.command()
        self.assertEqual(client.post(self.url("closures"), data, format="json").status_code, 201)
        User.objects.filter(id=self.technical.id).update(is_superuser=False)
        self.assertEqual(client.get(self.url("closure-preview")).status_code, 403)
        self.assertEqual(client.post(self.url("closures"), data, format="json").status_code, 403)
        User.objects.filter(id=self.technical.id).update(is_superuser=True)
        DeviceSession.objects.filter(id=self.live.id).update(revoked_at=self.registry.created_at)
        self.assertEqual(client.post(self.url("closures"), data, format="json").status_code, 401)
        self.assertEqual(APIClient().get(self.url("closure-preview")).status_code, 401)
        self.assertEqual(backups.RecoveryClosure.objects.count(), 1)

    def test_closure_operation_uuid_cannot_be_reused_as_a_native_business_operation(self):
        data = self.command()
        self.close(data)
        with self.assertRaises(OperationConflict):
            original_receipt(uuid.UUID(data["operation_id"]), self.a, self.owner,
                             "stock_opened", {})
