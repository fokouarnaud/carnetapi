"""Real purchase recovery responses conform to their published API contracts."""
import json
import uuid

from django.test import TestCase
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, FormatChecker

from backups.tests import test_recovery_purchases as fixtures
from purchasing.models import PurchaseDocument


def nullable_schema(value):
    """Express OpenAPI 3 nullable fields in the validator's JSON Schema dialect."""
    if isinstance(value, list):
        return [nullable_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: nullable_schema(item) for key, item in value.items() if key != "nullable"}
    return {"anyOf": [result, {"type": "null"}]} if value.get("nullable") else result


class RecoveryPurchaseContractTests(TestCase, fixtures.PurchaseSources):
    setUp = fixtures.RecoveryPurchaseTests.setUp
    authenticate = fixtures.RecoveryPurchaseTests.authenticate
    generation = fixtures.RecoveryPurchaseTests.generation
    record = fixtures.RecoveryPurchaseTests.record
    device = fixtures.RecoveryPurchaseTests.device
    review = fixtures.RecoveryPurchaseTests.review
    payload = fixtures.RecoveryPurchaseTests.payload
    url = fixtures.RecoveryPurchaseTests.url
    capture = fixtures.RecoveryPurchaseTests.capture
    preview = fixtures.RecoveryPurchaseTests.preview
    apply = fixtures.RecoveryPurchaseTests.apply
    native_url = fixtures.RecoveryPurchaseTests.native_url
    native_body = fixtures.RecoveryPurchaseTests.native_body
    native_supplier = fixtures.RecoveryPurchaseTests.native_supplier
    supplier_source = fixtures.RecoveryPurchaseTests.supplier_source
    purchase_url = fixtures.RecoveryPurchaseTests.purchase_url
    native_purchase = fixtures.RecoveryPurchaseTests.native_purchase

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def assert_response_schema(self, response, ending, method="get"):
        path = next(path for path in self.schema["paths"] if path.endswith(ending))
        rule = self.schema["paths"][path][method]["responses"][str(response.status_code)]["content"]["application/json"]["schema"]
        validator = Draft4Validator(
            nullable_schema({**rule, "components": self.schema["components"]}), format_checker=FormatChecker())
        errors = list(validator.iter_errors(json.loads(response.content)))
        self.assertEqual([(list(error.absolute_path), error.message) for error in errors], [])

    def test_order_and_invoice_preview_integration_replay_and_current_native_reads_match_openapi(self):
        for kind in ("order", "invoice"):
            source = self.purchase_source(kind)
            capture = self.capture(source)
            preview = self.client.get(self.url(f"operations/{capture.id}/integration-preview/"))
            self.assertEqual(preview.status_code, 200, preview.content)
            self.assertTrue(preview.data["can_apply"])
            self.assertEqual(preview.data["operation_type"], "purchase_" + kind)
            with self.subTest(kind=kind, response="new preview"):
                self.assert_response_schema(preview, "/integration-preview/")
            integrated, command = self.apply(capture, preview.data)
            self.assertEqual(integrated.status_code, 201, integrated.content)
            self.assertEqual(integrated.data["business_receipt"]["kind"], kind)
            with self.subTest(kind=kind, response="integration receipt"):
                self.assert_response_schema(integrated, "/integrate/", "post")
            replay = self.client.post(self.url(f"operations/{capture.id}/integrate/"), command, format="json")
            self.assertEqual((replay.status_code, replay.data), (200, integrated.data))
            with self.subTest(kind=kind, response="integration replay"):
                self.assert_response_schema(replay, "/integrate/", "post")
            known = self.client.get(self.url(f"operations/{capture.id}/integration-preview/"))
            self.assertEqual((known.status_code, known.data["outcome"]), (200, "already_known"))
            with self.subTest(kind=kind, response="known preview"):
                self.assert_response_schema(known, "/integration-preview/")
            for identifier, ending in ((None, kind + "s/"), (source.payload["purchase"]["id"], kind + "s/{id}/")):
                current = self.client.get(self.purchase_url(kind, identifier))
                self.assertEqual(current.status_code, 200, current.content)
                with self.subTest(kind=kind, response="native detail" if identifier else "native list"):
                    self.assert_response_schema(current, "/suppliers/{supplier_id}/" + ending)

    def test_native_creation_receipts_and_lists_and_details_match_openapi_for_both_document_types(self):
        for kind in ("order", "invoice"):
            source = self.purchase_source(kind)
            created = self.client.post(self.purchase_url(kind), self.native_body(source), format="json")
            self.assertEqual(created.status_code, 201, created.content)
            with self.subTest(kind=kind, response="native creation"):
                self.assert_response_schema(created, "/suppliers/{supplier_id}/" + kind + "s/", "post")
            for identifier, ending in ((None, kind + "s/"), (created.data["id"], kind + "s/{id}/")):
                current = self.client.get(self.purchase_url(kind, identifier))
                self.assertEqual(current.status_code, 200, current.content)
                with self.subTest(kind=kind, response="native detail" if identifier else "native list"):
                    self.assert_response_schema(current, "/suppliers/{supplier_id}/" + ending)

    def test_missing_supplier_or_order_parent_keeps_blocked_previews_and_refusals_schema_valid(self):
        for kind, missing in (("order", "supplier"), ("invoice", "order")):
            source = self.purchase_source(kind)
            source.payload["original_request"][missing + "_id"] = str(uuid.uuid4())
            source.payload["purchase"][missing + "_id"] = source.payload["original_request"][missing + "_id"]
            capture = self.capture(source)
            blocked = self.client.get(self.url(f"operations/{capture.id}/integration-preview/"))
            self.assertEqual(blocked.status_code, 200, blocked.content)
            self.assertFalse(blocked.data["can_apply"])
            self.assertEqual(blocked.data["outcome"], "blocked")
            self.assertIn("missing_" + missing, {row["code"] for row in blocked.data["issues"]})
            with self.subTest(kind=kind, response="blocked preview"):
                self.assert_response_schema(blocked, "/integration-preview/")
            refused, _ = self.apply(capture, blocked.data)
            self.assertEqual(refused.status_code, 409, refused.content)
            with self.subTest(kind=kind, response="blocked integration"):
                self.assert_response_schema(refused, "/integrate/", "post")
        self.assertFalse(PurchaseDocument.objects.exists())

    def test_inconsistent_order_and_invoice_errors_are_served_in_french_and_english(self):
        for kind in ("order", "invoice"):
            source = self.purchase_source(kind)
            source.payload["purchase"]["total_fcfa"] = 9999
            capture = self.capture(source)
            for language, detail in (
                ("en", "Recovery purchase document snapshot is inconsistent."),
                ("fr", "Les données figées de la commande ou de la facture fournisseur récupérée sont incohérentes."),
            ):
                with self.subTest(kind=kind, language=language):
                    response = self.client.get(self.url(f"operations/{capture.id}/integration-preview/"),
                                               HTTP_ACCEPT_LANGUAGE=language)
                    self.assertEqual((response.status_code, response.data["code"], str(response.data["detail"])),
                                     (400, "recovery_purchase_invalid", detail))
                    self.assert_response_schema(response, "/integration-preview/")
        self.assertFalse(PurchaseDocument.objects.exists())
