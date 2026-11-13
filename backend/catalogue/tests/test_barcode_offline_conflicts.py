"""US-11 / G14: a duplicate born offline becomes a catalogue conflict; scanning lists candidates, never picks one."""
import uuid

from django.test import TestCase

from catalogue.models import ProductPack
from catalogue.tests import test_barcodes as barcodes


class BarcodeOfflineConflictTests(TestCase):
    setUp = barcodes.BarcodeApiTests.setUp
    authenticate = barcodes.BarcodeApiTests.authenticate
    payload = barcodes.BarcodeApiTests.payload
    create = barcodes.BarcodeApiTests.create
    url = barcodes.BarcodeApiTests.url
    target_url = barcodes.BarcodeApiTests.target_url
    assignment = barcodes.BarcodeApiTests.assignment
    assign = barcodes.BarcodeApiTests.assign
    lookup = barcodes.BarcodeApiTests.lookup

    def offline(self, product, code, pack=None, operation_id=None):
        body = {"operation_id": operation_id or str(uuid.uuid4()), "base_version": 0, "code": code,
                "reason": "Code saisi hors connexion", "recorded_offline": True}
        return self.client.patch(self.target_url(product, pack), body, format="json")

    def test_offline_duplicate_is_recorded_as_pending_conflict_and_replay_is_stable(self):
        from catalogue.models import BarcodeCodeConflict
        product, _ = self.create()
        pack = ProductPack.objects.create(product_id=product["id"], name="Carton", base_quantity_milli=24000, sale_price_fcfa=4800)
        self.assertEqual(self.assign(product["id"], "EAN-1").status_code, 200)
        operation = str(uuid.uuid4())
        first = self.offline(product["id"], "EAN-1", pack=pack.id, operation_id=operation)
        self.assertEqual(first.status_code, 202, first.content)
        self.assertEqual(first.data["state"], "pending")
        replay = self.offline(product["id"], "EAN-1", pack=pack.id, operation_id=operation)
        self.assertEqual((replay.status_code, replay.data), (200, first.data))
        self.assertEqual(BarcodeCodeConflict.objects.count(), 1)

    def test_scan_of_conflicting_code_lists_candidates_without_choosing(self):
        product, _ = self.create()
        pack = ProductPack.objects.create(product_id=product["id"], name="Carton", base_quantity_milli=24000, sale_price_fcfa=4800)
        self.assign(product["id"], "EAN-2")
        self.offline(product["id"], "EAN-2", pack=pack.id)
        response = self.lookup("EAN-2")
        self.assertEqual(response.status_code, 300)
        self.assertEqual(response.data["code"], "barcode_ambiguous")
        self.assertEqual({row["pack_id"] for row in response.data["candidates"]}, {None, str(pack.id)})

    def test_online_duplicate_is_still_refused(self):
        product, _ = self.create()
        pack = ProductPack.objects.create(product_id=product["id"], name="Carton", base_quantity_milli=24000, sale_price_fcfa=4800)
        self.assign(product["id"], "EAN-3")
        self.assertEqual(self.assign(product["id"], "EAN-3", pack=pack.id).status_code, 409)


class BarcodeConflictResolutionTests(TestCase):
    setUp = barcodes.BarcodeApiTests.setUp
    authenticate = barcodes.BarcodeApiTests.authenticate
    payload = barcodes.BarcodeApiTests.payload
    create = barcodes.BarcodeApiTests.create
    url = barcodes.BarcodeApiTests.url
    target_url = barcodes.BarcodeApiTests.target_url
    assignment = barcodes.BarcodeApiTests.assignment
    assign = barcodes.BarcodeApiTests.assign
    lookup = barcodes.BarcodeApiTests.lookup
    offline = BarcodeOfflineConflictTests.offline

    def conflict(self, code):
        product, _ = self.create()
        pack = ProductPack.objects.create(product_id=product["id"], name="Carton", base_quantity_milli=24000, sale_price_fcfa=4800)
        self.assign(product["id"], code)
        return product, pack, self.offline(product["id"], code, pack=pack.id).data["conflict_id"]

    def resolve(self, conflict_id, decision):
        data = {"operation_id": str(uuid.uuid4()), "decision": decision, "reason": "Vérifié en rayon"}
        return self.client.post(f"/api/v1/shops/{self.shop.id}/barcode-conflicts/{conflict_id}/resolve/", data, format="json")

    def test_keep_leaves_the_registered_format_and_ends_ambiguity(self):
        product, pack, conflict_id = self.conflict("EAN-K")
        self.assertEqual(self.resolve(conflict_id, "keep").status_code, 201)
        found = self.lookup("EAN-K")
        self.assertEqual((found.status_code, found.data["pack_id"]), (200, None))

    def test_reassign_moves_the_code_to_the_offline_proposal(self):
        product, pack, conflict_id = self.conflict("EAN-R")
        self.assertEqual(self.resolve(conflict_id, "reassign").status_code, 201)
        found = self.lookup("EAN-R")
        self.assertEqual((found.status_code, found.data["pack_id"]), (200, str(pack.id)))
        self.assertEqual(self.resolve(conflict_id, "keep").status_code, 409)
