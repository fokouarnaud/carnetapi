"""US-09/10: editing a pack preserves billed history and rejects stale catalogue data."""
import uuid
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from catalogue.models import ProductPack
from catalogue.tests import test_packs as pack_tests


class PackChangeTests(TestCase):
    setUp = pack_tests.PackTests.setUp
    authenticate = pack_tests.PackTests.authenticate
    url = pack_tests.PackTests.url
    payload = pack_tests.PackTests.payload
    create = pack_tests.PackTests.create

    def test_pack_has_a_positive_durable_version(self):
        pack = ProductPack.objects.create(product=self.product, name="Carton", base_quantity_milli=24000, sale_price_fcfa=4800)
        self.assertEqual(pack.version, 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProductPack.objects.filter(id=pack.id).update(version=0)

    def change(self, **values):
        return {"operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Conditionnement corrigé", **values}

    def detail(self, pack):
        return self.url() + str(pack["id"]) + "/"

    def test_manager_edit_is_versioned_audited_and_has_no_stock_effect(self):
        from common.models import Activity
        from stock.models import StockMovement
        pack = self.create().data
        body = self.change(name="Carton de douze", base_quantity_milli=12000, sale_price_fcfa=2600)
        changed = self.client.patch(self.detail(pack), body, format="json")
        self.assertEqual(changed.status_code, 200, changed.content)
        self.assertEqual(changed.data["version"], 2)
        self.assertEqual(changed.data["product_version"], 2)
        self.assertEqual(changed.data["base_quantity_milli"], 12000)
        self.assertEqual(changed.data["sale_price_fcfa"], 2600)
        self.assertEqual(self.client.get(self.detail(pack)).data, changed.data)
        self.assertEqual(self.client.patch(self.detail(pack), body, format="json").data, changed.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.version, 2)
        self.assertEqual(self.product.sale_price_fcfa, 250)
        self.assertEqual(StockMovement.objects.count(), 0)
        audit = Activity.objects.get(action="pack_updated")
        self.assertEqual(audit.details["before"]["base_quantity_milli"], 24000)
        self.assertEqual(audit.details["after"]["name"], "Carton de douze")
        self.assertEqual(audit.reason, "Conditionnement corrigé")

    def test_stale_version_or_reused_operation_never_overwrites_newer_pack(self):
        pack = self.create().data
        body = self.change(sale_price_fcfa=4600)
        original = self.client.patch(self.detail(pack), body, format="json")
        self.assertEqual(original.status_code, 200)
        self.assertEqual(self.client.patch(self.detail(pack), self.change(sale_price_fcfa=4400), format="json").status_code, 409)
        self.assertEqual(self.client.patch(self.detail(pack), {**body, "sale_price_fcfa": 4500}, format="json").status_code, 409)
        newer = self.client.patch(self.detail(pack), self.change(base_version=2, sale_price_fcfa=4200), format="json")
        self.assertEqual(newer.status_code, 200)
        self.assertEqual(self.client.patch(self.detail(pack), body, format="json").data, original.data)
        self.assertEqual(self.client.get(self.detail(pack)).data["sale_price_fcfa"], 4200)

    def test_seller_may_read_but_cannot_edit_or_retry_manager_change(self):
        pack = self.create().data
        body = self.change(is_active=False)
        self.assertEqual(self.client.patch(self.detail(pack), body, format="json").status_code, 200)
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.detail(pack)).status_code, 200)
        for data in (body, self.change(name="Autre")):
            self.assertEqual(self.client.patch(self.detail(pack), data, format="json").status_code, 403)

    def test_edit_rejects_fractional_piece_factor_and_missing_change_or_reason(self):
        pack = self.create().data
        for changes in ({"base_quantity_milli": 1500}, {"base_quantity_milli": 0},
                        {"sale_price_fcfa": 1.5}, {"is_active": "false"}, {"name": " "},
                        {"name": "Autre", "reason": ""}, {}, {"product_id": str(self.weight.id)}):
            with self.subTest(changes=changes):
                response = self.client.patch(self.detail(pack), self.change(**changes), format="json")
                self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(ProductPack.objects.get(id=pack["id"]).version, 1)

    def test_edit_keeps_barcode_projection_and_scan_uses_new_factor(self):
        pack = self.create(self.payload(barcode="CARTON-24")).data
        response = self.client.patch(self.detail(pack), self.change(base_quantity_milli=12000, sale_price_fcfa=2600), format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["barcode"], "CARTON-24")
        self.assertEqual(response.data["barcode_version"], 1)
        scan = self.client.get(f"/api/v1/shops/{self.shop.id}/barcode-lookup/?code=CARTON-24")
        self.assertEqual(scan.status_code, 200)
        self.assertEqual(scan.data["base_quantity_milli"], 12000)
        self.assertEqual(scan.data["sale_price_fcfa"], 2600)
        self.assertEqual(scan.data["product_version"], 2)
        archived = self.client.patch(self.detail(pack), self.change(base_version=2, is_active=False), format="json")
        self.assertEqual(archived.status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/shops/{self.shop.id}/barcode-lookup/?code=CARTON-24").status_code, 404)

    def test_manager_history_preserves_creation_and_before_after(self):
        pack = self.create().data
        self.assertEqual(self.client.patch(self.detail(pack), self.change(name="Paquet"), format="json").status_code, 200)
        history = self.client.get(self.detail(pack) + "history/")
        self.assertEqual(history.status_code, 200, history.content)
        self.assertEqual(history.data["count"], 2)
        updated = next(event for event in history.data["results"] if event["action"] == "pack_updated")
        self.assertEqual(updated["details"]["before"]["name"], "Carton")
        self.assertEqual(updated["details"]["after"]["name"], "Paquet")
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.detail(pack) + "history/").status_code, 403)

    def test_pack_change_preserves_sale_snapshot_and_rejects_stale_catalogue(self):
        from sales.models import Sale
        from stock.models import StockMovement
        pack = self.create().data
        sales = f"/api/v1/shops/{self.shop.id}/sales/"
        def sale(version, amount):
            return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": version,
                           "pack_id": pack["id"], "quantity_milli": 1000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": amount}]}
        original = sale(1, 4800)
        billed = self.client.post(sales, original, format="json")
        self.assertEqual(billed.status_code, 201, billed.content)
        self.assertEqual(self.client.patch(self.detail(pack), self.change(name="Douzaine", base_quantity_milli=12000,
                         sale_price_fcfa=2600), format="json").status_code, 200)
        self.assertEqual(self.client.post(sales, original, format="json").data, billed.data)
        historical = self.client.get(sales + original["id"] + "/")
        self.assertEqual(historical.data["lines"][0]["factor_milli"], 24000)
        self.assertEqual(historical.data["lines"][0]["unit_price_fcfa"], 4800)
        self.assertEqual(historical.data["lines"][0]["unit_label"], "Carton")
        self.assertEqual(self.client.post(sales, sale(1, 4800), format="json").status_code, 409)
        self.assertEqual(Sale.objects.count(), 1)
        current = self.client.post(sales, sale(2, 2600), format="json")
        self.assertEqual(current.status_code, 201)
        self.assertEqual(current.data["lines"][0]["base_quantity_milli"], 12000)
        self.assertEqual(sorted(StockMovement.objects.values_list("quantity_milli", flat=True)), [-24000, -12000])

    def test_edit_is_scoped_to_product_shop_and_rechecks_current_session(self):
        from identity.models import DeviceSession
        from catalogue.tests.test_catalogue_sessions import CatalogueSessionTests
        pack = self.create().data
        wrong_product = self.url(self.weight) + str(pack["id"]) + "/"
        wrong_shop = self.url(self.foreign, self.other) + str(pack["id"]) + "/"
        for endpoint in (wrong_product, wrong_shop):
            self.assertEqual(self.client.patch(endpoint, self.change(name="Autre"), format="json").status_code, 404)
            self.assertEqual(self.client.get(endpoint).status_code, 404)
        with CatalogueSessionTests.revocation(self):
            blocked = self.client.patch(self.detail(pack), self.change(name="Autre"), format="json")
        self.assertEqual(blocked.status_code, 401)
        self.assertEqual(ProductPack.objects.get(id=pack["id"]).version, 1)

    def test_weight_factor_can_change_at_milli_precision(self):
        pack = self.create(product=self.weight).data
        endpoint = self.url(self.weight) + str(pack["id"]) + "/"
        response = self.client.patch(endpoint, self.change(base_quantity_milli=1250), format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["base_quantity_milli"], 1250)

    def test_purchase_receipt_uses_original_factor_after_pack_edit(self):
        from purchasing.models import Supplier
        from stock.models import StockMovement
        pack = self.create().data
        supplier = Supplier.objects.create(shop=self.shop, name="Fournisseur")
        invoices = f"/api/v1/shops/{self.shop.id}/suppliers/{supplier.id}/invoices/"
        invoice = self.client.post(invoices, {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "occurred_at": "2026-01-01T00:00:00Z", "reason": "Facture réelle",
            "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "pack_id": pack["id"],
                       "quantity_milli": 2000, "unit_price_fcfa": 1000}]}, format="json")
        self.assertEqual(invoice.status_code, 201, invoice.content)
        self.assertEqual(self.client.patch(self.detail(pack), self.change(name="Douzaine", base_quantity_milli=12000), format="json").status_code, 200)
        reception = self.client.post(invoices + str(invoice.data["id"]) + "/receipts/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-02T00:00:00Z",
            "reason": "Carton physiquement reçu", "lines": [{"line_id": invoice.data["lines"][0]["id"], "quantity_milli": 1000}]}, format="json")
        self.assertEqual(reception.status_code, 201, reception.content)
        self.assertEqual(StockMovement.objects.get().quantity_milli, 24000)
        original = self.client.get(invoices + str(invoice.data["id"]) + "/")
        self.assertEqual(original.data["lines"][0]["unit_label"], "Carton")
        self.assertEqual(original.data["lines"][0]["factor_milli"], 24000)
        self.assertEqual(original.data["lines"][0]["unit_price_fcfa"], 1000)

    def test_edit_conflict_and_no_change_errors_are_localized_in_english(self):
        pack = self.create().data
        stale = self.client.patch(self.detail(pack), self.change(base_version=2, name="Autre"), format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["code"], "pack_version_conflict")
        self.assertEqual(stale.json()["detail"], "The pack has changed. Read its current version.")
        empty = self.client.patch(self.detail(pack), self.change(), format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(empty.json()["non_field_errors"], ["No pack change was proposed."])

    def test_seller_edit_denial_names_the_action_in_both_languages(self):
        pack = self.create().data
        self.authenticate(self.seller)
        for language, message in (("fr", "Modification de conditionnement réservée au responsable."),
                                  ("en", "Only a manager can edit a pack.")):
            with self.subTest(language=language):
                response = self.client.patch(self.detail(pack), self.change(name="Autre"), format="json", HTTP_ACCEPT_LANGUAGE=language)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["detail"], message)

    def test_sqlite_rejects_fractional_stock_factors_money_and_versions(self):
        pack = ProductPack.objects.create(product=self.weight, name="Sachet", base_quantity_milli=1250, sale_price_fcfa=1100)
        for column, value in (("base_quantity_milli", 1250.5), ("sale_price_fcfa", 1100.5), ("version", 1.5)):
            with self.subTest(column=column), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f"UPDATE core_productpack SET {column} = %s WHERE id = %s", [value, pack.id.hex])
