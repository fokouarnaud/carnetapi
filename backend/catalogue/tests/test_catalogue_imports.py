"""US-40: durable catalogue previews, with no product or stock before confirmation."""
import hashlib
import uuid
from django.db import IntegrityError, transaction
from django.test import TestCase
from stock.tests import test_stock_thresholds as threshold_tests


class CatalogueImportTests(TestCase):
    setUp = threshold_tests.StockThresholdApiTests.setUp
    authenticate = threshold_tests.StockThresholdApiTests.authenticate

    def source(self):
        return b"name,unit_kind,unit_label,sale_price_fcfa\nSavon,piece,piece,250\n"

    def test_import_keeps_source_and_rejects_empty_oversized_or_inconsistent_receipt(self):
        from catalogue.models import CatalogueImport
        from identity.models import DeviceSession
        installation = DeviceSession.objects.get(installation__user=self.owner).installation
        def fields(source):
            return dict(shop=self.shop, actor=self.owner, installation=installation,
                        source=source, source_sha256=hashlib.sha256(source).hexdigest())
        record = CatalogueImport.objects.create(**fields(self.source()))
        record.refresh_from_db()
        self.assertEqual(bytes(record.source), self.source())
        for source in (b"", b"x" * 2000001):
            with self.subTest(size=len(source)), self.assertRaises(IntegrityError), transaction.atomic():
                CatalogueImport.objects.create(**fields(source))
        with self.assertRaises(IntegrityError), transaction.atomic():
            CatalogueImport.objects.filter(id=record.id).update(response={"applied": True})

    def endpoint(self, suffix="", shop=None):
        return f"/api/v1/shops/{(shop or self.shop).id}/catalogue-imports/" + suffix

    def upload(self, source=None, identifier=None):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.client.post(self.endpoint(), {"id": identifier or str(uuid.uuid4()),
            "file": SimpleUploadedFile("catalogue.csv", source if source is not None else self.source(), content_type="text/csv")}, format="multipart")

    def test_preview_is_durable_and_has_no_product_stock_or_cash_effect(self):
        from catalogue.models import CatalogueImport, Product
        from stock.models import StockMovement
        from cash.models import CashMovement
        identifier = str(uuid.uuid4())
        response = self.upload(identifier=identifier)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["id"], identifier)
        self.assertEqual(response.data["row_count"], 1)
        self.assertEqual(response.data["valid_count"], 1)
        self.assertFalse(response.data["has_errors"])
        self.assertEqual(response.data["rows"][0]["data"]["sale_price_fcfa"], 250)
        self.assertEqual(self.client.get(self.endpoint(identifier + "/")).data, response.data)
        self.assertEqual(self.upload(identifier=identifier).data, response.data)
        self.assertEqual(self.upload(self.source().replace(b"250", b"251"), identifier).status_code, 409)
        self.assertEqual(CatalogueImport.objects.count(), 1)
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(StockMovement.objects.count(), 0)
        self.assertEqual(CashMovement.objects.count(), 0)

    def test_hundred_rows_with_three_invalid_rows_are_reported_without_importing(self):
        from catalogue.models import Product
        rows = [f"Produit {number},piece,piece,{'1.5' if number in (3, 7, 80) else '250'}" for number in range(100)]
        source = ("name,unit_kind,unit_label,sale_price_fcfa\n" + "\n".join(rows) + "\n").encode()
        response = self.upload(source)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["row_count"], 100)
        self.assertEqual(response.data["valid_count"], 97)
        self.assertTrue(response.data["has_errors"])
        self.assertEqual([row["line"] for row in response.data["rows"] if row["errors"]], [5, 9, 82])
        self.assertEqual(self.confirm(response.data).status_code, 409)
        self.assertEqual(Product.objects.count(), 0)

    def test_seller_and_foreign_shop_cannot_read_a_preview(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201)
        identifier = str(response.data["id"])
        self.assertEqual(self.client.get(self.endpoint(identifier + "/", self.other)).status_code, 404)
        self.authenticate(self.seller)
        self.assertEqual(self.upload().status_code, 403)
        self.assertEqual(self.client.get(self.endpoint(identifier + "/")).status_code, 403)

    def confirmation(self, preview, **changes):
        return {"operation_id": str(uuid.uuid4()), "source_sha256": preview["source_sha256"],
                "reason": "Reprise physique vérifiée", **changes}

    def confirm(self, preview, body=None):
        return self.client.post(self.endpoint(str(preview["id"]) + "/confirm/"), body or self.confirmation(preview), format="json")

    def test_confirmation_applies_opening_and_threshold_once_without_debt_or_cash(self):
        from catalogue.models import Product
        from stock.models import StockMovement, StockAlertEvent
        from cash.models import CashMovement
        from purchasing.models import SupplierDebt
        from common.models import Activity
        source = ("name,unit_kind,unit_label,sale_price_fcfa,stock_threshold_milli,barcode,opening_quantity_milli,opening_unit_cost_fcfa,opening_occurred_at,opening_reason\n"
                  "Savon,piece,piece,250,8000,001234,10000,,2026-01-01T10:00:00Z,Stock compté\n"
                  "Farine,weight,kg,1000,500,,250,600.123456,2026-01-01T10:00:00Z,Pesée réelle\n").encode()
        preview = self.upload(source)
        self.assertEqual(preview.status_code, 201)
        self.assertFalse(preview.data["has_errors"], preview.data)
        body = self.confirmation(preview.data)
        applied = self.confirm(preview.data, body)
        self.assertEqual(applied.status_code, 200, applied.content)
        self.assertEqual(applied.data["created_count"], 2)
        self.assertEqual(self.confirm(preview.data, body).data, applied.data)
        self.assertEqual(self.confirm(preview.data).status_code, 409)
        self.assertEqual(Product.objects.count(), 2)
        self.assertEqual(sorted(StockMovement.objects.values_list("quantity_milli", flat=True)), [250, 10000])
        self.assertEqual(StockMovement.objects.get(product__name="Farine").unit_cost_micros, 600123456)
        self.assertIsNone(StockMovement.objects.get(product__name="Savon").unit_cost_micros)
        self.assertEqual(Product.objects.get(name="Savon").stock_threshold_milli, 8000)
        self.assertEqual(StockAlertEvent.objects.count(), 1)
        self.assertEqual(Activity.objects.filter(action="catalogue_import_applied").count(), 1)
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(SupplierDebt.objects.exists())

    def test_confirmation_refuses_every_row_if_any_row_is_invalid(self):
        from catalogue.models import Product
        from stock.models import StockMovement
        preview = self.upload(b"name,unit_kind,unit_label,sale_price_fcfa\nSavon,piece,piece,250\nFarine,weight,kg,1.5\n")
        self.assertEqual(preview.status_code, 201)
        applied = self.confirm(preview.data)
        self.assertEqual(applied.status_code, 409, applied.content)
        self.assertEqual(applied.json()["code"], "catalogue_import_invalid")
        self.assertEqual(applied.json()["preview"]["valid_count"], 1)
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_confirmation_revalidates_code_claimed_since_preview(self):
        from catalogue.models import Barcode, Product
        source = b"name,unit_kind,unit_label,sale_price_fcfa,barcode\nSavon,piece,piece,250,00123\n"
        preview = self.upload(source)
        self.assertFalse(preview.data["has_errors"], preview.data)
        existing = Product.objects.create(shop=self.shop, name="Existant", unit_kind="piece", unit_label="piece", sale_price_fcfa=100)
        Barcode.objects.create(shop=self.shop, product=existing, code="00123")
        applied = self.confirm(preview.data)
        self.assertEqual(applied.status_code, 409)
        self.assertEqual(Product.objects.count(), 1)
        self.assertTrue(self.client.get(self.endpoint(str(preview.data["id"]) + "/")).data["has_errors"])

    def test_real_collision_on_second_row_rolls_back_the_first_product_and_alert(self):
        from catalogue.models import CatalogueImport, Product
        from stock.models import StockMovement, StockAlertEvent
        from common.models import Activity
        source = b"name,unit_kind,unit_label,sale_price_fcfa,stock_threshold_milli\nPremier,piece,piece,250,0\nSecond,piece,piece,250,0\n"
        preview = self.upload(source)
        self.assertFalse(preview.data["has_errors"], preview.data)
        blocked_id = uuid.uuid5(uuid.UUID(str(preview.data["id"])), "product:3")
        Product.objects.create(id=blocked_id, shop=self.other, name="UUID existant", unit_kind="piece", unit_label="piece", sale_price_fcfa=1)
        applied = self.confirm(preview.data)
        self.assertEqual(applied.status_code, 409)
        self.assertEqual(Product.objects.filter(shop=self.shop).count(), 0)
        self.assertFalse(StockMovement.objects.exists())
        self.assertFalse(StockAlertEvent.objects.exists())
        self.assertFalse(Activity.objects.exists())
        self.assertIsNone(CatalogueImport.objects.get().applied_operation_id)

    def test_invalid_csv_headers_encoding_shape_and_empty_data_cannot_be_confirmed(self):
        samples = [b"name,name,unit_kind,unit_label,sale_price_fcfa\nA,B,piece,piece,1\n",
                   b"name,unit_kind,unit_label,sale_price_fcfa,unexpected\nA,piece,piece,1,x\n",
                   b"name,unit_kind,unit_label,sale_price_fcfa\n",
                   b"name,unit_kind,unit_label,sale_price_fcfa\nA,piece,piece,1,extra\n",
                   b"name,unit_kind,unit_label,sale_price_fcfa\n\xff,piece,piece,1\n"]
        for source in samples:
            with self.subTest(source=source):
                response = self.upload(source)
                self.assertEqual(response.status_code, 201)
                self.assertTrue(response.data["has_errors"], response.data)
                self.assertEqual(self.confirm(response.data).status_code, 409)

    def test_upload_bounds_and_row_limit_are_technical_limits_not_partial_imports(self):
        from catalogue.models import CatalogueImport, Product
        # G20: 2 MB and 5 000 data rows per file.
        self.assertEqual(self.upload(b"x" * 2000001).status_code, 400)
        self.assertFalse(CatalogueImport.objects.exists())
        self.assertEqual(self.upload(b"x" * 1500000).status_code, 201)  # within 2 MB: previewed, never partially applied
        CatalogueImport.objects.all().delete()
        accepted = self.upload(b"name,unit_kind,unit_label,sale_price_fcfa\n" + b"A,piece,piece,1\n" * 5000)
        self.assertEqual((accepted.status_code, accepted.data["file_errors"]), (201, []))
        source = b"name,unit_kind,unit_label,sale_price_fcfa\n" + b"A,piece,piece,1\n" * 5001
        preview = self.upload(source)
        self.assertEqual(preview.status_code, 201)
        self.assertTrue(preview.data["has_errors"])
        self.assertEqual(preview.data["file_errors"][0]["code"], "too_many_rows")
        self.assertEqual(self.confirm(preview.data).status_code, 409)
        self.assertFalse(Product.objects.exists())

    def test_template_is_usable_and_only_a_current_manager_can_download_it(self):
        template = self.client.get(self.endpoint("template/"))
        self.assertEqual(template.status_code, 200)
        self.assertIn("text/csv", template["Content-Type"])
        preview = self.upload(template.content)
        self.assertEqual(preview.status_code, 201)
        self.assertFalse(preview.data["has_errors"], preview.data)
        self.assertGreater(preview.data["row_count"], 0)
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.endpoint("template/")).status_code, 403)

    def test_confirmation_rechecks_rights_hash_and_session_before_replay(self):
        from catalogue.models import CatalogueImport, Product
        from identity.models import DeviceSession
        from django.utils import timezone
        preview = self.upload().data
        wrong = self.confirm(preview, self.confirmation(preview, source_sha256="0" * 64))
        self.assertEqual(wrong.status_code, 409)
        self.assertEqual(wrong.json()["code"], "catalogue_import_source_conflict")
        body = self.confirmation(preview)
        applied = self.confirm(preview, body)
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(self.confirm(preview, {**body, "reason": "Autre"}).status_code, 409)
        self.authenticate(self.seller)
        self.assertEqual(self.confirm(preview, body).status_code, 403)
        self.authenticate(self.owner)
        DeviceSession.objects.filter(installation__user=self.owner).update(revoked_at=timezone.now())
        self.assertEqual(self.confirm(preview, body).status_code, 401)
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(CatalogueImport.objects.count(), 1)

    def test_csv_template_accept_header_and_english_errors_are_supported(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        response = self.client.get(self.endpoint("template/"), HTTP_ACCEPT="text/csv", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn(b"Sample product", response.content)
        invalid = self.client.post(self.endpoint(), {"id": str(uuid.uuid4()),
            "file": SimpleUploadedFile("bad.csv", b"wrong,header\n")}, format="multipart", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(invalid.status_code, 201)
        self.assertEqual(invalid.data["file_errors"][0]["detail"], "Invalid CSV header. Use the provided template.")
        self.authenticate(self.seller)
        denied = self.client.get(self.endpoint("template/"), HTTP_ACCEPT="text/csv")
        self.assertEqual(denied.status_code, 403)
        self.assertIn("application/json", denied["Content-Type"])

    def test_applied_preview_does_not_become_invalid_when_catalogue_codes_change_later(self):
        from catalogue.models import Barcode, Product
        preview = self.upload(b"name,unit_kind,unit_label,sale_price_fcfa,barcode\nSavon,piece,piece,250,00123\n").data
        body = self.confirmation(preview)
        applied = self.confirm(preview, body)
        self.assertEqual(applied.status_code, 200)
        other = Product.objects.create(shop=self.shop, name="Autre", unit_kind="piece", unit_label="piece", sale_price_fcfa=1)
        Barcode.objects.filter(code="00123").update(product=other)
        historical = self.client.get(self.endpoint(str(preview["id"]) + "/"))
        self.assertEqual(historical.status_code, 200)
        self.assertTrue(historical.data["applied"])
        self.assertFalse(historical.data["has_errors"], historical.data)
        self.assertEqual(self.confirm(preview, body).data, applied.data)

    def test_duplicate_file_codes_and_foreign_category_are_row_errors(self):
        from catalogue.models import Category, Product
        category = Category.objects.create(shop=self.other, name="Autre boutique")
        source = ("name,unit_kind,unit_label,sale_price_fcfa,barcode,category_id\n"
                  "A,piece,piece,1,0001,\nB,piece,piece,1,0001,\n"
                  f"C,piece,piece,1,,{category.id}\n").encode()
        preview = self.upload(source)
        self.assertEqual(preview.status_code, 201)
        self.assertEqual(preview.data["valid_count"], 1)
        self.assertIn("barcode", preview.data["rows"][1]["errors"])
        self.assertIn("category_id", preview.data["rows"][2]["errors"])
        self.assertEqual(self.confirm(preview.data).status_code, 409)
        self.assertFalse(Product.objects.exists())

    def test_preview_preserves_large_cost_to_the_exact_millionth(self):
        source = b"name,unit_kind,unit_label,sale_price_fcfa,opening_quantity_milli,opening_unit_cost_fcfa,opening_occurred_at,opening_reason\nA,weight,kg,1,1000,9223372036854.775807,2026-01-01T10:00:00Z,Compte\n"
        preview = self.upload(source)
        self.assertEqual(preview.status_code, 201)
        self.assertFalse(preview.data["has_errors"], preview.data)
        self.assertEqual(preview.data["rows"][0]["data"]["opening"]["unit_cost_fcfa"], "9223372036854.775807")

    def test_oversized_upload_stops_spooling_before_the_csv_disk_bound(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from django.core.files.uploadhandler import TemporaryFileUploadHandler
        original = TemporaryFileUploadHandler.receive_data_chunk
        sizes = []
        def observe_write(handler, chunk, start):
            result = original(handler, chunk, start)
            sizes.append(handler.file.tell())
            return result
        with TemporaryDirectory() as directory, self.settings(FILE_UPLOAD_MAX_MEMORY_SIZE=0, FILE_UPLOAD_TEMP_DIR=directory):
            with patch.object(TemporaryFileUploadHandler, "receive_data_chunk", new=observe_write):
                response = self.upload(b"x" * 3000000)  # G20: beyond the 2 MB bound
            self.assertEqual(response.status_code, 400)
            self.assertLessEqual(max(sizes, default=0), 2000000)
            self.assertEqual(list(Path(directory).iterdir()), [])
