"""US-09: a product owns one bounded, versioned optional photo."""
from django.db import IntegrityError, transaction
from django.test import TestCase
from catalogue.models import Product
from identity.models import Shop, User


class PhotoModelTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(username="photo-model")
        shop = Shop.objects.create(name="Images", owner=owner)
        self.product = Product.objects.create(shop=shop, name="Savon", unit_kind="piece", unit_label="pièce", sale_price_fcfa=250)

    def test_storage_preserves_bytes_and_enforces_20000_byte_bound(self):
        from catalogue.models import ProductPhoto
        photo = ProductPhoto.objects.create(product=self.product, data=b"x" * 20000, width=640, height=480)
        photo.refresh_from_db()
        self.assertEqual(bytes(photo.data), b"x" * 20000)
        self.assertEqual(photo.size_bytes, 20000)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProductPhoto.objects.update(data=b"x" * 20001)

    def test_optional_photo_cannot_have_empty_content_or_inconsistent_dimensions(self):
        from catalogue.models import ProductPhoto
        photo = ProductPhoto.objects.create(product=self.product)
        self.assertIsNone(photo.data)
        self.assertEqual(photo.size_bytes, 0)
        for changes in ({"data": b""}, {"data": b"image", "width": 0, "height": 1},
                        {"data": None, "width": 1}, {"version": 0}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                ProductPhoto.objects.update(**changes)

import hashlib
from io import BytesIO
import uuid
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from stock.tests import test_stock_thresholds as threshold_tests


class PhotoApiTests(TestCase):
    setUp = threshold_tests.StockThresholdApiTests.setUp
    authenticate = threshold_tests.StockThresholdApiTests.authenticate
    payload = threshold_tests.StockThresholdApiTests.payload
    create = threshold_tests.StockThresholdApiTests.create
    url = threshold_tests.StockThresholdApiTests.url

    def image_bytes(self, size=(1200, 900), orientation=None):
        image = Image.new("RGB", size, "#245A9C")
        output = BytesIO()
        exif = Image.Exif()
        if orientation:
            exif[274] = orientation
            exif[270] = "Private camera description"
        image.save(output, format="JPEG", quality=95, exif=exif)
        return output.getvalue()

    def upload(self, product, image=None, version=0, operation_id=None):
        body = {"operation_id": operation_id or str(uuid.uuid4()), "base_version": version,
                "reason": "Photo produit", "image": SimpleUploadedFile("phone.jpg", image or self.image_bytes(), content_type="image/jpeg")}
        return self.client.post(self.url(product) + "photo/", body, format="multipart")

    def test_upload_compresses_bounds_and_serves_photo_only_with_shop_access(self):
        product, _ = self.create()
        response = self.upload(product["id"])
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(response.data["size_bytes"], 20000)
        self.assertEqual(response.data["version"], 1)
        self.member.can_read_cost = False
        self.member.save()
        self.authenticate(self.seller)
        image = self.client.get(self.url(product["id"]) + "photo/")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image["Content-Type"], "image/jpeg")
        self.assertEqual(hashlib.sha256(image.content).hexdigest(), response.data["sha256"])
        with Image.open(BytesIO(image.content)) as decoded:
            decoded.load()
            self.assertLessEqual(max(decoded.size), 640)
        self.assertEqual(self.client.get(self.url(product["id"]) + "photo/", {"version": 999}).status_code, 409)
        self.assertEqual(self.client.get(self.url(product["id"], self.other) + "photo/").status_code, 404)
        self.assertEqual(self.upload(product["id"]).status_code, 403)

    def test_retry_and_clear_keep_tombstone_version_without_modifying_stock(self):
        from common.models import Activity
        from catalogue.models import ProductPhoto
        from stock.models import StockMovement
        product, _ = self.create()
        operation = str(uuid.uuid4())
        source = self.image_bytes()
        first = self.upload(product["id"], source, operation_id=operation)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.upload(product["id"], source, operation_id=operation).data, first.data)
        self.assertEqual(self.upload(product["id"], self.image_bytes((10, 10)), operation_id=operation).status_code, 409)
        self.assertEqual(self.upload(product["id"], source).status_code, 409)
        clear = {"operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Retrait photo"}
        result = self.client.delete(self.url(product["id"]) + "photo/", clear, format="json")
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.data["present"])
        self.assertEqual(result.data["version"], 2)
        self.assertEqual(self.client.get(self.url(product["id"]) + "photo/").status_code, 404)
        self.assertIsNone(ProductPhoto.objects.get().data)
        self.assertEqual(self.upload(product["id"], source, operation_id=operation).data, first.data)
        self.assertEqual(Activity.objects.filter(action="product_photo_updated").count(), 1)
        self.assertFalse(StockMovement.objects.exists())

    def test_invalid_input_and_exif_orientation(self):
        from catalogue.models import ProductPhoto
        product, _ = self.create()
        self.assertEqual(self.upload(product["id"], b"not an image").status_code, 400)
        self.assertFalse(ProductPhoto.objects.exists())
        response = self.upload(product["id"], self.image_bytes((40, 20), orientation=6))
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.data["width"], response.data["height"]), (20, 40))
        stored = self.client.get(self.url(product["id"]) + "photo/")
        with Image.open(BytesIO(stored.content)) as image:
            self.assertFalse(image.getexif())

    def test_catalogue_exposes_photo_metadata_and_version_after_removal(self):
        product, _ = self.create()
        initial = self.client.get(self.url(product["id"]))
        self.assertEqual(initial.data["photo"]["version"], 0)
        self.assertFalse(initial.data["photo"]["present"])
        uploaded = self.upload(product["id"])
        self.assertEqual(uploaded.status_code, 200)
        listed = self.client.get(self.url()).data["results"][0]["photo"]
        self.assertEqual(listed["sha256"], uploaded.data["sha256"])
        self.assertEqual(listed["version"], 1)
        self.assertNotIn("data", listed)
        removed = self.client.delete(self.url(product["id"]) + "photo/", {"operation_id": str(uuid.uuid4()),
            "base_version": 1, "reason": "Retrait"}, format="json")
        self.assertEqual(removed.status_code, 200)
        current = self.client.get(self.url(product["id"]))
        self.assertEqual(current.data["photo"]["version"], 2)
        self.assertFalse(current.data["photo"]["present"])

    def test_openapi_describes_binary_multipart_upload_and_json_removal(self):
        from drf_spectacular.generators import SchemaGenerator
        schema = SchemaGenerator().get_schema(request=None, public=True)
        path = schema["paths"]["/api/v1/shops/{shop_id}/products/{product_id}/photo/"]
        content = path["post"]["requestBody"]["content"]
        self.assertEqual(set(content), {"multipart/form-data"})
        reference = content["multipart/form-data"]["schema"]["$ref"].split("/")[-1]
        image = schema["components"]["schemas"][reference]["properties"]["image"]
        self.assertEqual(image["format"], "binary")
        self.assertIn("application/json", path["delete"]["requestBody"]["content"])

    def test_photo_download_accepts_image_media_type_and_errors_stay_json(self):
        product, _ = self.create()
        self.assertEqual(self.upload(product["id"]).status_code, 200)
        response = self.client.get(self.url(product["id"]) + "photo/", HTTP_ACCEPT="image/jpeg")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        missing = self.client.get(self.url(product["id"], self.other) + "photo/", HTTP_ACCEPT="image/jpeg")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing["Content-Type"], "application/json")

    def test_noisy_png_is_compressed_and_decode_bombs_are_rejected(self):
        import random
        from unittest.mock import patch
        from catalogue.models import ProductPhoto
        product, _ = self.create()
        output = BytesIO()
        image = Image.frombytes("RGB", (1000, 1000), random.Random(8).randbytes(3000000))
        image.save(output, format="PNG")
        result = self.upload(product["id"], output.getvalue())
        self.assertEqual(result.status_code, 200)
        self.assertLessEqual(result.data["size_bytes"], 20000)
        self.assertGreaterEqual(min(result.data["width"], result.data["height"]), 64)
        with patch.object(Image, "MAX_IMAGE_PIXELS", 10):
            refused = self.upload(product["id"], self.image_bytes((40, 20)), version=1)
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(ProductPhoto.objects.get().version, 1)

    def test_corrupt_png_is_rejected_without_photo_or_audit(self):
        from common.models import Activity
        from catalogue.models import ProductPhoto
        product, _ = self.create()
        output = BytesIO()
        Image.new("RGB", (16, 16), "blue").save(output, format="PNG")
        corrupt = bytearray(output.getvalue())
        position = corrupt.index(b"IDAT") + 4
        corrupt[position] ^= 1
        response = self.upload(product["id"], bytes(corrupt))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ProductPhoto.objects.exists())
        self.assertFalse(Activity.objects.filter(action="product_photo_updated").exists())

    def test_embedded_camera_comment_is_removed(self):
        product, _ = self.create()
        source = BytesIO()
        Image.new("RGB", (64, 48), "blue").save(source, format="JPEG", comment=b"Private camera location")
        with Image.open(BytesIO(source.getvalue())) as original:
            self.assertIn("comment", original.info)
        self.assertEqual(self.upload(product["id"], source.getvalue()).status_code, 200)
        downloaded = self.client.get(self.url(product["id"]) + "photo/")
        with Image.open(BytesIO(downloaded.content)) as normalized:
            self.assertNotIn("comment", normalized.info)

    def test_photo_changes_are_visible_in_manager_catalogue_history(self):
        product, _ = self.create()
        self.assertEqual(self.upload(product["id"]).status_code, 200)
        history_url = self.url(product["id"]) + "history/"
        history = self.client.get(history_url)
        self.assertEqual(history.status_code, 200)
        self.assertEqual([event["action"] for event in history.data["results"]],
                         ["product_created", "product_photo_updated"])
        self.assertEqual(history.data["results"][1]["reason"], "Photo produit")
        removed = self.client.delete(self.url(product["id"]) + "photo/", {"operation_id": str(uuid.uuid4()),
            "base_version": 1, "reason": "Retrait de la photo"}, format="json")
        self.assertEqual(removed.status_code, 200)
        events = self.client.get(history_url).data["results"]
        self.assertEqual(events[-1]["action"], "product_photo_removed")
        self.assertEqual(events[-1]["reason"], "Retrait de la photo")
        self.assertFalse(events[-1]["details"]["after"]["present"])
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(history_url).status_code, 403)

    def test_catalogue_query_count_does_not_grow_per_photo(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        for index in range(4):
            product, _ = self.create(name=f"Image {index}")
            if index % 2 == 0:
                self.assertEqual(self.upload(product["id"]).status_code, 200)
        with CaptureQueriesContext(connection) as single_queries:
            one = self.client.get(self.url(), {"page_size": 1})
        with CaptureQueriesContext(connection) as all_queries:
            many = self.client.get(self.url(), {"page_size": 4})
        self.assertEqual(one.status_code, 200)
        self.assertEqual(many.status_code, 200)
        self.assertEqual(len(many.data["results"]), 4)
        self.assertEqual(len(single_queries), len(all_queries))
        self.assertEqual([row["photo"]["present"] for row in many.data["results"]], [True, False, True, False])

    def test_oversized_photo_stops_temporary_writes_at_the_source_bound(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from django.core.files.uploadhandler import TemporaryFileUploadHandler
        from catalogue.models import ProductPhoto
        from common.models import Activity
        product, _ = self.create()
        original = TemporaryFileUploadHandler.receive_data_chunk
        sizes = []
        def observe_write(handler, chunk, start):
            result = original(handler, chunk, start)
            sizes.append(handler.file.tell())
            return result
        with TemporaryDirectory() as directory, self.settings(FILE_UPLOAD_MAX_MEMORY_SIZE=0, FILE_UPLOAD_TEMP_DIR=directory):
            with patch.object(TemporaryFileUploadHandler, "receive_data_chunk", new=observe_write):
                response = self.upload(product["id"], b"x" * 11000000)
            self.assertEqual(response.status_code, 400)
            self.assertLessEqual(max(sizes, default=0), 10000000)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertFalse(ProductPhoto.objects.exists())
        self.assertFalse(Activity.objects.filter(action="product_photo_updated").exists())
