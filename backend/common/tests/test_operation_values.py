"""Nullable operation facts keep JSON null instead of breaking receipt comparisons."""
from django.test import SimpleTestCase
from common.services.operations import json_value


class OperationJsonValueTests(SimpleTestCase):
    def test_null_source_fact_can_be_compared_without_an_empty_http_body(self):
        self.assertIsNone(json_value(None))
        self.assertEqual(json_value({"before_due_at":None}),{"before_due_at":None})
        self.assertIs(json_value(False),False)
        self.assertEqual(json_value(0),0)
