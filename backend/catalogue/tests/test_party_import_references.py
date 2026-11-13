"""US-40 / G20: `ref_import` is unique per shop and type; a duplicate blocks the whole file."""
from django.test import TestCase

from catalogue.tests import test_party_imports as party


class PartyImportReferenceTests(TestCase):
    setUp = party.PartyImportTests.setUp
    authenticate = party.PartyImportTests.authenticate
    url = party.PartyImportTests.url
    upload = party.PartyImportTests.upload
    confirm = party.PartyImportTests.confirm

    def test_reference_is_stored_and_reused_reference_is_refused_in_a_later_file(self):
        from customers.models import Customer
        preview = self.upload("name;ref_import\nAwa;CLI-001\nBella;CLI-002\n".encode())
        self.assertFalse(preview.data["has_errors"], preview.data)
        self.assertEqual(self.confirm(preview.data).status_code, 200)
        self.assertEqual(set(Customer.objects.values_list("import_reference", flat=True)), {"CLI-001", "CLI-002"})
        again = self.upload("name;ref_import\nAwa bis;CLI-001\n".encode())
        self.assertTrue(again.data["has_errors"])
        self.assertIn("ref_import", again.data["rows"][0]["errors"])

    def test_duplicate_reference_inside_one_file_blocks_every_row(self):
        from customers.models import Customer
        preview = self.upload("name;ref_import\nAwa;CLI-9\nAwa T.;CLI-9\n".encode())
        self.assertTrue(preview.data["has_errors"])
        self.assertEqual(self.confirm(preview.data).status_code, 409)
        self.assertFalse(Customer.objects.exists())
