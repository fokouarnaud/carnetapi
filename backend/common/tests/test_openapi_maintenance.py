"""The documented global outage response must match the HTTP middleware."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase, RequestFactory, override_settings
from drf_spectacular.generators import SchemaGenerator
from jsonschema import validate


class OpenApiMaintenanceTests(SimpleTestCase):
    def test_all_documented_api_actions_describe_the_real_maintenance_response(self):
        from backups.middleware import RestoreGateMiddleware
        schema = SchemaGenerator().get_schema(request=None, public=True)
        methods = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
        operations = [operation for item in schema["paths"].values()
                      for method, operation in item.items() if method in methods]
        self.assertGreater(len(operations), 0)
        for operation in operations:
            with self.subTest(operation=operation["operationId"]):
                self.assertIn("503", operation["responses"])
                outage = operation["responses"]["503"]
                self.assertIn("Retry-After", outage["headers"])
                self.assertIn("Cache-Control", outage["headers"])
        with TemporaryDirectory(prefix="openapi-maintenance-") as directory:
            with override_settings(RESTORE_GATE_PATH=Path(directory) / "missing.sqlite3"):
                response = RestoreGateMiddleware(lambda request: self.fail("handler must not run"))(
                    RequestFactory().get("/api/v1/shops/"))
        self.assertEqual(response.status_code, 503)
        documented = operations[0]["responses"]["503"]["content"]["application/json"]["schema"]
        validate(json.loads(response.content), {**documented, "components": schema["components"]})
