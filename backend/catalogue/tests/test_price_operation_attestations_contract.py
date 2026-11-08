"""Operation receipts declare bounded attestations without rewriting legacy shapes."""
from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator


class PriceOperationAttestationSchemaTests(SimpleTestCase):
    def test_all_native_write_receipts_declare_the_bounded_operation_delta(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        product = "/api/v1/shops/{shop_id}/products/{product_id}/price-conflicts/"
        pack = "/api/v1/shops/{shop_id}/products/{product_id}/packs/{pack_id}/price-conflicts/"
        for root in (product, pack):
            for suffix in ("", "{id}/resolve/"):
                responses = schema["paths"][root + suffix]["post"]["responses"]
                for status in ("200",) if suffix else ("200", "201"):
                    with self.subTest(path=root + suffix, status=status):
                        rule = responses[status]["content"]["application/json"]["schema"]
                        component = schema["components"]["schemas"][rule["$ref"].rsplit("/", 1)[-1]]
                        self.assertIn("operation_snapshot", component["properties"])
                        self.assertEqual("operation_snapshot" in component.get("required", []), status == "201")
                        snapshot = component["properties"]["operation_snapshot"]
                        snapshot = schema["components"]["schemas"][snapshot["$ref"].rsplit("/", 1)[-1]]
                        self.assertEqual(snapshot["properties"]["proposals_created"]["maxItems"], 2)
        for name in ("ProductPriceConflict", "PackPriceConflict", "PriceConflictPreview", "PackPriceConflictPreview"):
            self.assertNotIn("operation_snapshot", schema["components"]["schemas"][name]["properties"])
