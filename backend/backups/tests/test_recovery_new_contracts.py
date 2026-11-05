"""Validate real new recovery and native responses against the published oneOfs."""
import json

from django.test import TestCase
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, FormatChecker

from backups.tests import test_recovery_catalogue_revisions as catalogue_fixtures
from backups.tests import test_recovery_purchase_receipts as receipt_fixtures
from backups.tests.test_recovery_purchase_contract import nullable_schema


class NewRecoveryContractTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def check_response(self, response, ending, method="get"):
        path = next(path for path in self.schema["paths"] if path.endswith(ending))
        rule = self.schema["paths"][path][method]["responses"][str(response.status_code)]["content"]["application/json"]["schema"]
        validator = Draft4Validator(nullable_schema({**rule, "components": self.schema["components"]}),
                                    format_checker=FormatChecker())
        errors = [(list(error.absolute_path), error.message) for error in validator.iter_errors(json.loads(response.content))]
        self.assertEqual(errors, [])

    def verify_capture(self, case, source):
        capture = case.capture(source)
        response = case.client.get(case.url(f"operations/{capture.id}/integration-preview/"))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.data["can_apply"], response.data)
        self.check_response(response, "/integration-preview/")
        integrated, command = case.apply(capture, response.data)
        self.assertEqual(integrated.status_code, 201, integrated.content)
        self.check_response(integrated, "/integrate/", "post")
        repeated = case.client.post(case.url(f"operations/{capture.id}/integrate/"), command, format="json")
        self.assertEqual((repeated.status_code, repeated.data), (200, integrated.data))
        self.check_response(repeated, "/integrate/", "post")

    def test_purchase_receipt_recovery_and_native_list_keep_schema_valid_exact_proofs(self):
        case = receipt_fixtures.RecoveryPurchaseReceiptTests()
        case.setUp()
        _, invoice = case.native_purchase()
        self.verify_capture(case, case.receipt_source(invoice))
        listing = case.client.get(case.purchase_url("invoice", invoice["id"], "receipts/"))
        self.assertEqual(listing.status_code, 200, listing.content)
        self.check_response(listing, "/invoices/{document_id}/receipts/")
        source = case.receipt_source(invoice, 4000)
        body = case.native_body(source)
        body.pop("document_id")
        native = case.client.post(case.purchase_url("invoice", invoice["id"], "receipts/"), body, format="json")
        self.assertEqual(native.status_code, 201, native.content)
        self.check_response(native, "/invoices/{document_id}/receipts/", "post")

    def test_product_revision_preview_and_receipt_keep_schema_valid_without_stock_mutation(self):
        case = catalogue_fixtures.RecoveryCatalogueRevisionTests()
        case.setUp()
        self.verify_capture(case, case.product_source(case.before()))

    def test_pack_revision_preview_and_receipt_publish_the_source_parent_timestamp(self):
        case = catalogue_fixtures.RecoveryCatalogueRevisionTests()
        case.setUp()
        self.verify_capture(case, case.pack_source(case.before(pack=True)))

    def test_inconsistent_physical_purchase_receipt_explains_the_error_in_both_languages(self):
        case = receipt_fixtures.RecoveryPurchaseReceiptTests()
        case.setUp()
        _, invoice = case.native_purchase()
        source = case.receipt_source(invoice)
        source.payload['receipt']['lines'][0]['cost_basis']['total_cost_nanos_fcfa'] = '1'
        capture = case.capture(source)
        for language, detail in (
                ('en', 'Recovery physical purchase receipt snapshot is inconsistent.'),
                ('fr', 'Les données figées de la réception physique fournisseur récupérée sont incohérentes.')):
            with self.subTest(language=language):
                response = case.client.get(case.url(f'operations/{capture.id}/integration-preview/'),
                    HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual((response.status_code, response.data['code'], str(response.data['detail'])),
                    (400, 'recovery_purchase_receipt_invalid', detail))
                self.check_response(response, '/integration-preview/')
