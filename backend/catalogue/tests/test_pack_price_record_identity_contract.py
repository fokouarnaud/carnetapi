"""New record attestations are required without rewriting legacy receipt schemas."""
import json

from django.test import SimpleTestCase, TestCase
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, FormatChecker

from backups.tests.test_recovery_purchase_contract import nullable_schema
from catalogue.tests import test_pack_price_record_identity as fixtures


def record_rules():
    schema = SchemaGenerator().get_schema(request=None, public=True)
    path = "/api/v1/shops/{shop_id}/products/{product_id}/packs/{pack_id}/price-conflicts/"
    responses = schema["paths"][path]["post"]["responses"]
    return schema, {status: responses[str(status)]["content"]["application/json"]["schema"] for status in (200, 201)}


class PackPriceRecordIdentitySchemaTests(SimpleTestCase):
    def test_created_receipt_requires_own_proposal_while_legacy_retry_can_omit_it(self):
        schema, rules = record_rules()
        resolved = {status: schema["components"]["schemas"][rule["$ref"].rsplit("/", 1)[-1]]
            for status, rule in rules.items()}
        self.assertIn("recorded_proposal", resolved[201].get("required", []))
        self.assertIn("recorded_proposal", resolved[200]["properties"])
        self.assertNotIn("recorded_proposal", resolved[200].get("required", []))
        for rule in resolved.values():
            self.assertEqual(rule["properties"]["proposals"]["maxItems"], 25)
        self.assertNotIn("recorded_proposal", schema["components"]["schemas"]["PackPriceConflict"]["properties"])


class PackPriceRecordIdentityContractTests(TestCase):
    setUp = fixtures.PackPriceRecordIdentityTests.setUp
    authenticate = fixtures.PackPriceRecordIdentityTests.authenticate
    url = fixtures.PackPriceRecordIdentityTests.url
    payload = fixtures.PackPriceRecordIdentityTests.payload
    create = fixtures.PackPriceRecordIdentityTests.create
    pack_url = fixtures.PackPriceRecordIdentityTests.pack_url
    conflicts_url = fixtures.PackPriceRecordIdentityTests.conflicts_url
    command = fixtures.PackPriceRecordIdentityTests.command
    post_record = fixtures.PackPriceRecordIdentityTests.post_record
    freeze_legacy_receipt = fixtures.PackPriceRecordIdentityTests.freeze_legacy_receipt

    def test_real_created_new_retry_and_legacy_retry_match_their_published_contracts(self):
        schema, rules = record_rules()
        command = self.command()
        created = self.post_record(command)
        retry = self.manager_api.post(self.conflicts_url(), command, format="json")
        self.assertEqual((retry.status_code, retry.data), (200, created.data))
        self.freeze_legacy_receipt(command, created)
        old = self.manager_api.post(self.conflicts_url(), command, format="json")
        self.assertEqual(old.status_code, 200, old.content)
        for response in (created, retry, old):
            with self.subTest(status=response.status_code, has_own="recorded_proposal" in response.data):
                rule = {**rules[response.status_code], "components": schema["components"]}
                errors = list(Draft4Validator(nullable_schema(rule), format_checker=FormatChecker())
                    .iter_errors(json.loads(response.content)))
                self.assertEqual([error.message for error in errors], [])
        self.assertNotIn("recorded_proposal", old.data)
