"""US-11: one confirmed barcode target per shop, across products and packs."""
from django.db import IntegrityError, transaction
from django.test import TestCase
from catalogue.models import Product, ProductPack
from identity.models import Shop, User


class BarcodeModelTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="barcode-model")
        self.shop = Shop.objects.create(name="Codes", owner=self.owner)
        self.other = Shop.objects.create(name="Autres", owner=self.owner)
        self.product = Product.objects.create(shop=self.shop, name="Savon", unit_kind="piece", unit_label="pièce", sale_price_fcfa=250)
        self.pack = ProductPack.objects.create(product=self.product, name="Carton", base_quantity_milli=24000, sale_price_fcfa=4800)

    def test_leading_zero_survives_and_duplicate_across_unit_and_pack_is_rejected(self):
        from catalogue.models import Barcode
        unit = Barcode.objects.create(shop=self.shop, product=self.product, code="0012345678905")
        unit.refresh_from_db()
        self.assertEqual(unit.code, "0012345678905")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Barcode.objects.create(shop=self.shop, product=self.product, pack=self.pack, code="0012345678905")
        other_product = Product.objects.create(shop=self.other, name="Autre", unit_kind="piece", unit_label="pièce", sale_price_fcfa=250)
        Barcode.objects.create(shop=self.other, product=other_product, code="0012345678905")
        self.assertEqual(Barcode.objects.count(), 2)

    def test_each_unit_and_pack_has_one_registration_even_when_code_is_cleared(self):
        from catalogue.models import Barcode
        Barcode.objects.create(shop=self.shop, product=self.product, code=None)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Barcode.objects.create(shop=self.shop, product=self.product, code="UNIT")
        Barcode.objects.create(shop=self.shop, product=self.product, pack=self.pack, code=None)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Barcode.objects.create(shop=self.shop, product=self.product, pack=self.pack, code="PACK")

    def test_empty_code_and_invalid_version_are_rejected_by_database(self):
        from catalogue.models import Barcode
        for changes in ({"code": ""}, {"code": "X", "version": 0}, {"code": "Y", "version": 1.5}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                if changes.get("version") == 1.5:
                    from django.db import connection
                    row = Barcode.objects.create(shop=self.shop, product=self.product, code="Y")
                    with connection.cursor() as cursor:
                        cursor.execute("UPDATE core_barcode SET version = %s WHERE id = %s", [1.5, row.id.hex])
                else:
                    Barcode.objects.create(shop=self.shop, product=self.product, **changes)

    def test_storage_rejects_codes_longer_than_the_api_bound(self):
        from catalogue.models import Barcode
        Barcode.objects.create(shop=self.shop, product=self.product, code="x" * 200)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Barcode.objects.update(code="x" * 201)


import uuid
from stock.tests import test_stock_thresholds as threshold_tests


class BarcodeApiTests(TestCase):
    setUp = threshold_tests.StockThresholdApiTests.setUp
    authenticate = threshold_tests.StockThresholdApiTests.authenticate
    payload = threshold_tests.StockThresholdApiTests.payload
    create = threshold_tests.StockThresholdApiTests.create
    url = threshold_tests.StockThresholdApiTests.url

    def target_url(self, product, pack=None, shop=None):
        base = self.url(product, shop)
        return base + "barcode/" if pack is None else base + f"packs/{pack}/barcode/"

    def assignment(self, code, version=0):
        return {"operation_id": str(uuid.uuid4()), "base_version": version, "code": code, "reason": "Code vérifié"}

    def assign(self, product, code, version=0, pack=None):
        return self.client.patch(self.target_url(product, pack), self.assignment(code, version), format="json")

    def lookup(self, code, shop=None):
        return self.client.get(f"/api/v1/shops/{(shop or self.shop).id}/barcode-lookup/", {"code": code})

    def test_unit_and_pack_assignment_select_exact_format_with_leading_zero(self):
        product, _ = self.create()
        pack = ProductPack.objects.create(product_id=product["id"], name="Carton", base_quantity_milli=24000, sale_price_fcfa=4800)
        self.assertEqual(self.assign(product["id"], "0012345678905").status_code, 200)
        self.assertEqual(self.assign(product["id"], "CARTON", pack=pack.id).status_code, 200)
        self.member.can_read_cost = False
        self.member.save()
        self.authenticate(self.seller)
        unit = self.lookup("0012345678905")
        self.assertEqual(unit.status_code, 200)
        self.assertEqual(unit.data["product_id"], product["id"])
        self.assertIsNone(unit.data["pack_id"])
        self.assertEqual(unit.data["base_quantity_milli"], 1000)
        self.assertEqual(unit.data["sale_price_fcfa"], 250)
        carton = self.lookup("CARTON")
        self.assertEqual(carton.data["pack_id"], str(pack.id))
        self.assertEqual(carton.data["base_quantity_milli"], 24000)
        self.assertEqual(carton.data["sale_price_fcfa"], 4800)
        self.assertNotIn("purchase_price_fcfa", carton.data)
        self.assertEqual(self.lookup("INCONNU").status_code, 404)

    def test_duplicate_between_unit_and_pack_has_no_stock_or_audit_effect(self):
        from catalogue.models import Barcode
        from stock.models import StockMovement
        product, _ = self.create()
        second, _ = self.create()
        pack = ProductPack.objects.create(product_id=second["id"], name="Carton", base_quantity_milli=24000, sale_price_fcfa=4800)
        self.assertEqual(self.assign(product["id"], "0012345678905").status_code, 200)
        collision = self.assign(second["id"], "0012345678905", pack=pack.id)
        self.assertEqual(collision.status_code, 409)
        self.assertEqual(collision.data["code"], "barcode_conflict")
        self.assertIsNone(collision.data["conflicting_pack_id"])
        self.assertEqual(Barcode.objects.exclude(code=None).count(), 1)
        from common.models import Activity
        self.assertEqual(Activity.objects.filter(action="barcode_assigned").count(), 1)
        self.assertFalse(StockMovement.objects.exists())

    def test_versioned_clear_retry_and_reassignment_preserve_original_receipt(self):
        from catalogue.models import Barcode
        from common.models import Activity
        product, _ = self.create()
        body = self.assignment("ORIGINAL")
        endpoint = self.target_url(product["id"])
        first = self.client.patch(endpoint, body, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(self.client.patch(endpoint, body, format="json").data, first.data)
        self.assertEqual(self.assign(product["id"], "AUTRE").status_code, 409)
        cleared = self.assign(product["id"], None, version=1)
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.data["version"], 2)
        self.assertEqual(self.lookup("ORIGINAL").status_code, 404)
        self.assertEqual(self.client.patch(endpoint, body, format="json").data, first.data)
        self.assertIsNone(Barcode.objects.get().code)
        audit = Activity.objects.get(id=body["operation_id"])
        self.assertEqual(audit.details["after"]["code"], "ORIGINAL")

    def test_seller_foreign_shop_and_invalid_codes_cannot_change_assignment(self):
        product, _ = self.create()
        for code in (True, 123456, 1.5, "", "x" * 201):
            with self.subTest(code=code):
                self.assertEqual(self.assign(product["id"], code).status_code, 400)
        self.authenticate(self.seller)
        self.assertEqual(self.assign(product["id"], "CODE").status_code, 403)
        self.assertEqual(self.lookup("CODE", self.other).status_code, 404)
        self.authenticate(self.owner)
        self.assertEqual(self.client.patch(self.target_url(product["id"], shop=self.other), self.assignment("CODE"), format="json").status_code, 404)

    def test_inconsistent_barcode_row_cannot_expose_foreign_product_or_pack(self):
        from catalogue.models import Barcode
        from django.utils import timezone
        product, _ = self.create()
        foreign = Product.objects.create(shop=self.other, name="Privé", unit_kind="piece", unit_label="pièce", sale_price_fcfa=999)
        foreign_pack = ProductPack.objects.create(product=foreign, name="Privé carton", base_quantity_milli=12000, sale_price_fcfa=9999)
        Barcode.objects.create(shop=self.shop, product=foreign, code="FOREIGN-UNIT")
        Barcode.objects.create(shop=self.shop, product_id=product["id"], pack=foreign_pack, code="FOREIGN-PACK")
        self.authenticate(self.seller)
        self.assertEqual(self.lookup("FOREIGN-UNIT").status_code, 404)
        self.assertEqual(self.lookup("FOREIGN-PACK").status_code, 404)

    def test_optional_codes_are_created_atomically_and_included_in_catalogue_cache(self):
        product, response = self.create(barcode="0012345678905")
        self.assertEqual(response.data["barcode"], "0012345678905")
        self.assertEqual(response.data["barcode_version"], 1)
        pack_payload = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Carton",
                        "base_quantity_milli": 24000, "sale_price_fcfa": 4800, "barcode": "CARTON"}
        pack_url = self.url(product["id"]) + "packs/"
        result = self.client.post(pack_url, pack_payload, format="json")
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(result.data["barcode"], "CARTON")
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.url(product["id"])).data["barcode"], "0012345678905")
        self.assertEqual(self.client.get(pack_url).data["results"][0]["barcode"], "CARTON")
        self.assertEqual(self.client.get(pack_url).data["results"][0]["barcode_version"], 1)

    def test_duplicate_during_creation_rolls_back_product_opening_and_registration(self):
        from common.models import Activity
        from catalogue.models import Barcode
        from stock.models import StockMovement
        self.create(barcode="CODE")
        body = self.payload(barcode="CODE", opening={"quantity_milli": 10000, "unit_cost_fcfa": None,
            "occurred_at": "2026-01-01T10:00:00Z", "reason": "Ouverture"})
        response = self.client.post(self.url(), body, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(Barcode.objects.count(), 1)
        self.assertFalse(StockMovement.objects.exists())
        self.assertEqual(Activity.objects.filter(action="product_created").count(), 1)

    def test_code_case_is_exact_and_archived_targets_are_not_selected(self):
        from catalogue.models import Barcode
        product, _ = self.create(barcode="AbC")
        self.assertEqual(self.lookup("abc").status_code, 404)
        self.assertEqual(self.lookup("AbC").status_code, 200)
        Product.objects.filter(id=product["id"]).update(is_active=False)
        self.assertEqual(self.lookup("AbC").status_code, 404)
        self.assertEqual(Barcode.objects.get().code, "AbC")
        self.assertEqual(self.assign(product["id"], None, version=1).status_code, 200)
        self.assertIsNone(Barcode.objects.get().code)

    def test_barcode_assignment_is_visible_to_cache_without_changing_tariff_version(self):
        product, _ = self.create()
        self.assertEqual(self.assign(product["id"], "A").status_code, 200)
        response = self.client.get(self.url(product["id"]))
        self.assertEqual(response.data["barcode"], "A")
        self.assertEqual(response.data["barcode_version"], 1)
        self.assertEqual(response.data["version"], 1)
        self.assertEqual(self.assign(product["id"], None, version=1).status_code, 200)
        response = self.client.get(self.url(product["id"]))
        self.assertIsNone(response.data["barcode"])
        self.assertEqual(response.data["barcode_version"], 2)

    def test_pre_barcode_receipts_are_replayable_without_rewriting_acknowledgement(self):
        from common.models import Activity
        product, _ = self.create()
        event = Activity.objects.get(id=product["operation_id"])
        event.details["request"].pop("barcode", None)
        event.details["response"].pop("barcode", None)
        event.details["response"].pop("barcode_version", None)
        old = event.details["response"].copy()
        event.save(update_fields=["details"])
        self.assertEqual(self.client.post(self.url(), product, format="json").data, old)

    def test_conflict_details_do_not_reveal_foreign_targets_from_inconsistent_rows(self):
        from catalogue.models import Barcode
        product, _ = self.create()
        foreign = Product.objects.create(shop=self.other, name="Privé", unit_kind="piece", unit_label="pièce", sale_price_fcfa=999)
        Barcode.objects.create(shop=self.shop, product=foreign, code="FOREIGN")
        result = self.assign(product["id"], "FOREIGN")
        self.assertEqual(result.status_code, 409)
        self.assertIsNone(result.data["conflicting_product_id"])
        self.assertIsNone(result.data["conflicting_pack_id"])
