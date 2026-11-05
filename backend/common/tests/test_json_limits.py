"""US-41: JSON is bounded before decoding, without partially committing commands."""
from io import BytesIO
import json
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.exceptions import APIException, ParseError
from rest_framework.settings import api_settings
from sales.tests import test_sales as sale_tests
from sales.models import Sale
from cash.models import CashMovement
from common.models import Activity


class ShortReadStream(BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.requests = []

    def read(self, size=-1):
        self.requests.append(size)
        return super().read(min(size, 3) if size >= 0 else size)


class JSONStreamLimitTests(SimpleTestCase):
    @override_settings(API_JSON_MAX_BYTES=64)
    def test_oversized_stream_without_length_stops_at_limit_plus_one(self):
        stream = ShortReadStream(b'{"name":"' + b'x' * 200 + b'"}')
        parser = api_settings.DEFAULT_PARSER_CLASSES[0]()
        with self.assertRaises(APIException) as raised:
            parser.parse(stream)
        self.assertEqual(raised.exception.status_code, 413)
        self.assertEqual(raised.exception.get_codes(), "json_body_too_large")
        self.assertEqual(stream.tell(), 65)
        self.assertTrue(all(0 < size <= 65 for size in stream.requests))

    @override_settings(API_JSON_MAX_BYTES=64)
    def test_short_reads_and_exact_byte_boundary_preserve_valid_json(self):
        # 9-byte prefix + 53 ASCII bytes + 2-byte suffix = 64 bytes.
        stream = ShortReadStream(b'{"name":"' + b'x' * 53 + b'"}')
        self.assertEqual(api_settings.DEFAULT_PARSER_CLASSES[0]().parse(stream), {"name": "x" * 53})

    def test_excessively_nested_json_is_a_parse_error(self):
        stream = BytesIO(b'[' * 10000 + b'0' + b']' * 10000)
        with self.assertRaises(ParseError):
            api_settings.DEFAULT_PARSER_CLASSES[0]().parse(stream)


    @override_settings(API_JSON_MAX_DEPTH=3)
    def test_nesting_boundary_and_brackets_inside_strings(self):
        parser = api_settings.DEFAULT_PARSER_CLASSES[0]()
        self.assertEqual(parser.parse(BytesIO(b'{"a":[{"b":"[[[["}]}')), {"a": [{"b": "[[[["}]})
        with self.assertRaises(ParseError):
            parser.parse(BytesIO(b'{"a":[{"b":[]}]}'))

    @override_settings(API_JSON_MAX_BYTES=100000)
    def test_wide_json_and_utf16_remain_valid_and_nan_is_refused(self):
        parser = api_settings.DEFAULT_PARSER_CLASSES[0]()
        source = json.dumps([{}] * 10000).encode()
        self.assertEqual(len(parser.parse(BytesIO(source))), 10000)
        self.assertEqual(parser.parse(BytesIO('{"name":"Pièce"}'.encode("utf-16")),
            parser_context={"encoding": "utf-16"}), {"name": "Pièce"})
        with self.assertRaises(ParseError):
            parser.parse(BytesIO(b'{"amount":NaN}'))


class JSONCommandLimitTests(TestCase):
    setUp = sale_tests.CashSaleTests.setUp
    authenticate = sale_tests.CashSaleTests.authenticate
    payload = sale_tests.CashSaleTests.payload
    url = sale_tests.CashSaleTests.url

    def test_oversized_valid_sale_is_refused_before_any_business_effect(self):
        body = json.dumps(self.payload()).encode() + b' ' * 1000001
        response = self.client.post(self.url(), body, content_type="application/json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(response.status_code, 413, response.content[:200])
        self.assertEqual(response.json()["code"], "json_body_too_large")
        self.assertEqual(response.json()["detail"], "JSON request body is too large.")
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(Activity.objects.filter(action="sale_created").exists())

    def test_exact_utf8_byte_boundary_is_accepted_and_one_extra_byte_is_refused(self):
        self.authenticate(self.owner)
        body = json.dumps(self.payload(occurred_at="2026-01-01T09:00:00Z", reason="Pièce retrouvée"), ensure_ascii=False).encode("utf-8")
        with override_settings(API_JSON_MAX_BYTES=len(body)):
            self.assertEqual(self.client.post(self.url(), body, content_type="application/json").status_code, 201)
            refused = self.client.post(self.url(), body + b' ', content_type="application/json")
            self.assertEqual(refused.status_code, 413)
        self.assertEqual(Sale.objects.count(), 1)

    @override_settings(API_JSON_MAX_BYTES=64)
    def test_import_route_cannot_bypass_limit_with_its_explicit_json_parser(self):
        self.authenticate(self.owner)
        response = self.client.post(f"/api/v1/shops/{self.shop.id}/catalogue-imports/", b'{}' + b' ' * 100, content_type="application/json")
        self.assertEqual(response.status_code, 413, response.content[:200])

    def test_authentication_still_precedes_parsing_an_oversized_private_command(self):
        self.client.credentials()
        response = self.client.post(self.url(), b'{}' + b' ' * 1000001, content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(Sale.objects.exists())
