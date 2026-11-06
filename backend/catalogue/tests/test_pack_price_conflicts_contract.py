"""Real pack-price conflict responses agree with the protected API schema."""
import json
import uuid

from django.test import SimpleTestCase, TestCase
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, FormatChecker

from backups.tests.test_recovery_purchase_contract import nullable_schema
from catalogue.tests import test_pack_price_conflicts_api as fixtures


class PackPriceConflictSchemaBoundaryTests(SimpleTestCase):
    def test_receipt_schema_declares_the_bounded_first_proposal_page(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        proposals = schema["components"]["schemas"]["PackPriceConflict"]["properties"]["proposals"]
        self.assertEqual(proposals.get("maxItems"), 25)


class PackPriceConflictContractTests(TestCase):
    setUp = fixtures.PackPriceConflictAPITests.setUp
    authenticate = fixtures.PackPriceConflictAPITests.authenticate
    url = fixtures.PackPriceConflictAPITests.url
    payload = fixtures.PackPriceConflictAPITests.payload
    create = fixtures.PackPriceConflictAPITests.create
    pack_url = fixtures.PackPriceConflictAPITests.pack_url
    conflicts_url = fixtures.PackPriceConflictAPITests.conflicts_url
    command = fixtures.PackPriceConflictAPITests.command
    record = fixtures.PackPriceConflictAPITests.record
    preview = fixtures.PackPriceConflictAPITests.preview

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def check_body(self, response, suffix, method="get", *, public_pack=False):
        path = next(path for path in self.schema["paths"]
            if (path.endswith("/packs/{id}/") if public_pack
                else "/packs/{pack_id}/price-conflicts/" in path and path.endswith(suffix)))
        rule = self.schema["paths"][path][method]["responses"][str(response.status_code)]["content"]["application/json"]["schema"]
        errors = list(Draft4Validator(nullable_schema({**rule, "components": self.schema["components"]}),
            format_checker=FormatChecker()).iter_errors(json.loads(response.content)))
        self.assertEqual([(list(error.absolute_path), error.message) for error in errors], [])

    def test_record_retry_list_detail_preview_resolve_and_vendor_metadata_match_openapi(self):
        registration = self.command()
        created = self.manager_api.post(self.conflicts_url(), registration, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        detail = self.conflicts_url(created.data["id"])
        rows = [(created, "/price-conflicts/", "post"),
            (self.manager_api.post(self.conflicts_url(), registration, format="json"), "/price-conflicts/", "post"),
            (self.manager_api.get(self.conflicts_url()), "/price-conflicts/", "get"),
            (self.manager_api.get(detail), "/price-conflicts/{id}/", "get"),
            (self.manager_api.get(detail + "proposals/"), "/price-conflicts/{id}/proposals/", "get"),
            (self.manager_api.get(detail + "preview/"), "/price-conflicts/{id}/preview/", "get")]
        for response, suffix, method in rows:
            with self.subTest(suffix=suffix, method=method):
                self.assertIn(response.status_code, (200, 201), response.content)
                self.check_body(response, suffix, method)
        preview = rows[-1][0].data
        self.assertLessEqual(len(preview["preview_token"]), 512)
        self.assertIn(":", preview["preview_token"])
        self.authenticate(self.seller)
        public = self.client.get(self.pack_url())
        self.assertEqual(public.status_code, 200, public.content)
        self.assertEqual(set(public.data["price_conflict"]), {"id", "state", "common_price_fcfa"})
        self.check_body(public, "", public_pack=True)
        body = {"operation_id": str(uuid.uuid4()), "preview_token": preview["preview_token"],
            "proposal_id": None, "reason": "Prix commun conservé explicitement"}
        resolved = self.manager_api.post(detail + "resolve/", body, format="json")
        self.assertEqual(resolved.status_code, 200, resolved.content)
        self.check_body(resolved, "/price-conflicts/{id}/resolve/", "post")
        retry = self.manager_api.post(detail + "resolve/", body, format="json")
        self.assertEqual((retry.status_code, retry.data), (200, resolved.data))
        self.check_body(retry, "/price-conflicts/{id}/resolve/", "post")
        closed = self.manager_api.get(detail + "preview/")
        self.assertEqual((closed.status_code, closed.data["can_resolve"], closed.data["preview_token"]), (200, False, None))
        self.check_body(closed, "/price-conflicts/{id}/preview/")
        self.assertIsNone(self.client.get(self.pack_url()).data["price_conflict"])
