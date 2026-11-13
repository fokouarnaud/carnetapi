"""US-40 / G20: UTF-8 with or without BOM, `;` or `,` detected, 5 000 data rows at most."""
from types import SimpleNamespace

from django.test import SimpleTestCase

from common.services.csv_sources import read_source


def record(text, bom=False):
    data = text.encode("utf-8")
    return SimpleNamespace(source=(b"\xef\xbb\xbf" + data) if bom else data)


class CsvSourceG20Tests(SimpleTestCase):
    def test_semicolon_separator_is_detected(self):
        rows, errors = read_source(record("name;phone\nAwa;+237690000000\n", bom=True), {"name"}, {"phone"})
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["data"], {"name": "Awa", "phone": "+237690000000"})

    def test_comma_separator_still_works_and_commas_inside_semicolon_files_are_kept(self):
        rows, errors = read_source(record("name,phone\nAwa,\n"), {"name"}, {"phone"})
        self.assertEqual((errors, rows[0]["data"]["name"]), ([], "Awa"))
        rows, errors = read_source(record("name;phone\nAwa, Bakery;\n"), {"name"}, {"phone"})
        self.assertEqual((errors, rows[0]["data"]["name"]), ([], "Awa, Bakery"))

    def test_five_thousand_rows_are_accepted_and_one_more_is_refused(self):
        body = "name\n" + "".join(f"Client {index}\n" for index in range(5000))
        rows, errors = read_source(record(body), {"name"}, set())
        self.assertEqual((len(rows), errors), (5000, []))
        rows, errors = read_source(record(body + "Client 5000\n"), {"name"}, set())
        self.assertEqual([error["code"] for error in errors], ["too_many_rows"])
