"""US-11: name fallback is shop-filtered and paginated before serialization."""
from django.test import TestCase
from stock.tests import test_stock_thresholds as threshold_tests


class CatalogueSearchTests(TestCase):
    setUp = threshold_tests.StockThresholdApiTests.setUp
    authenticate = threshold_tests.StockThresholdApiTests.authenticate
    payload = threshold_tests.StockThresholdApiTests.payload
    create = threshold_tests.StockThresholdApiTests.create
    url = threshold_tests.StockThresholdApiTests.url

    def test_partial_unicode_name_search_is_filtered_before_pagination(self):
        expected = []
        for name in ("ÉPICES Douala", "Savon", "Épices Yaoundé", "Épices Garoua"):
            product, _ = self.create(name=name)
            if name != "Savon":
                expected.append(product["id"])
        self.member.can_read_cost = False
        self.member.save()
        self.authenticate(self.seller)
        result = self.client.get(self.url(), {"search": "  épices  ", "page_size": 2})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["count"], 3)
        self.assertEqual([row["id"] for row in result.data["results"]], expected[:2])
        self.assertNotIn("purchase_price_fcfa", result.data["results"][0])
        second = self.client.get(result.data["next"])
        self.assertEqual([row["id"] for row in second.data["results"]], expected[2:])

    def test_empty_search_keeps_catalogue_and_wildcards_are_literal(self):
        self.create(name="Savon 100%")
        self.create(name="Savon simple")
        self.assertEqual(self.client.get(self.url(), {"search": "  "}).data["count"], 2)
        self.assertEqual(self.client.get(self.url(), {"search": "%"}).data["count"], 1)
        self.assertEqual(self.client.get(self.url(), {"search": "introuvable"}).data["count"], 0)
        self.assertEqual(self.client.get(self.url(), {"search": "x" * 151}).status_code, 400)

    def test_search_never_opens_foreign_shop_and_matches_pack_names_within_product(self):
        from catalogue.models import Product, ProductPack
        product, _ = self.create(name="Savon")
        Product.objects.create(shop=self.other, name="Savon étranger", unit_kind="piece", unit_label="pièce", sale_price_fcfa=250)
        ProductPack.objects.create(product_id=product["id"], name="Carton", base_quantity_milli=24000, sale_price_fcfa=4800)
        ProductPack.objects.create(product_id=product["id"], name="Paquet", base_quantity_milli=6000, sale_price_fcfa=1400)
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.url(), {"search": "Savon"}).data["count"], 1)
        self.assertEqual(self.client.get(self.url(shop=self.other), {"search": "Savon"}).status_code, 404)
        formats = self.client.get(self.url(product["id"]) + "packs/", {"search": "cart"})
        self.assertEqual(formats.data["count"], 1)
        self.assertEqual(formats.data["results"][0]["name"], "Carton")
