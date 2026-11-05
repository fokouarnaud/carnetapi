"""US-42: real consistent encrypted archives, restricted to technical operators."""
from datetime import timedelta
from contextlib import closing
from io import BytesIO
import json
import hashlib
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch
import uuid

from cryptography.fernet import Fernet
from django.core import mail
from django.db import OperationalError
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from backups.models import BackupEvent, BackupJob, BackupPart
from catalogue.models import Product, ProductPhoto
from identity.models import DeviceSession, Installation, Shop, User
from identity.services.authentication import DeviceJWTAuthentication


class BackupPreparationApiTests(TransactionTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="backup-api-test-")
        self.addCleanup(self.directory.cleanup)
        self.key = Fernet.generate_key()
        self.root = Path(self.directory.name) / "archives"
        self.options = override_settings(BACKUP_ROOT=self.root, BACKUP_ENCRYPTION_KEY=self.key,
            BACKUP_KEY_ID="test-primary", BACKUP_PART_MAX_BYTES=32768)
        self.options.enable()
        self.addCleanup(self.options.disable)
        self.technical = User.objects.create_superuser(username="backup-api-technical", password=None)
        self.owner = User.objects.create_user(username="backup-api-owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Boutique sauvegardée")
        self.product = Product.objects.create(shop=self.shop, name="Photo conservée", unit_kind="piece",
            unit_label="pièce", sale_price_fcfa=500)
        photo = BytesIO()
        Image.new("RGB", (32, 32), "blue").save(photo, format="JPEG")
        self.photo = photo.getvalue()
        ProductPhoto.objects.create(product=self.product, data=self.photo, width=32, height=32)
        self.client = APIClient()
        self.session = self.authenticate(self.technical)

    def authenticate(self, user):
        device = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=1))
        access = AccessToken.for_user(user)
        access["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return session

    def payload(self, **values):
        return {"operation_id": str(uuid.uuid4()), "reason": "Sauvegarde manuelle contrôlée",
            "recipient": "backup@example.test", **values}

    def prepare(self, body=None):
        response = self.client.post("/api/v1/backups/", body or self.payload(), format="json")
        self.assertEqual(response.status_code, 201, getattr(response, "data", None))
        return response.json()

    def test_prepared_archive_contains_consistent_database_and_photo_without_exposing_key_or_sending_mail(self):
        receipt = self.prepare()
        self.assertEqual(receipt["state"], "ready")
        self.assertGreater(receipt["part_count"], 0)
        self.assertEqual(len(receipt["parts"]), receipt["part_count"])
        self.assertNotIn(self.key.decode(), json.dumps(receipt))
        self.assertNotIn("file_path", json.dumps(receipt))
        paths = []
        for part in receipt["parts"]:
            path = self.root / receipt["id"] / part["filename"]
            self.assertEqual(path.stat().st_size, part["size_bytes"])
            self.assertLessEqual(part["size_bytes"], 32768)
            self.assertNotIn(self.key, path.read_bytes())
            paths.append(path)
        from backups.services.archive import verify_bundle
        verified = verify_bundle(paths, self.key, Path(self.directory.name) / "verified")
        with closing(sqlite3.connect(verified.path)) as database:
            self.assertEqual(database.execute("SELECT name FROM core_shop WHERE id=?", (self.shop.id.hex,)).fetchone()[0], self.shop.name)
            self.assertEqual(bytes(database.execute("SELECT data FROM core_productphoto WHERE product_id=?", (self.product.id.hex,)).fetchone()[0]), self.photo)
        self.assertEqual(len(getattr(mail, "outbox", [])), 0)
        self.assertEqual(list(BackupEvent.objects.values_list("kind", flat=True)), ["requested", "prepared"])
        self.assertFalse(list(self.root.rglob("*.sqlite3")))

    def test_retry_preserves_archive_uuid_parts_and_original_receipt_and_changed_body_conflicts(self):
        body = self.payload()
        original = self.prepare(body)
        hashes = list(BackupPart.objects.order_by("number").values_list("sha256", flat=True))
        retry = self.client.post("/api/v1/backups/", body, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json(), original)
        conflict = self.client.post("/api/v1/backups/", {**body, "recipient": "other@example.test"}, format="json")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(BackupJob.objects.count(), 1)
        self.assertEqual(list(BackupPart.objects.order_by("number").values_list("sha256", flat=True)), hashes)
        self.assertEqual(BackupEvent.objects.count(), 2)

    def test_part_download_checks_the_registered_size_and_digest(self):
        receipt = self.prepare()
        part = receipt["parts"][0]
        endpoint = f"/api/v1/backups/{receipt['id']}/parts/{part['number']}/"
        response = self.client.get(endpoint)
        self.assertEqual(response.status_code, 200)
        try:
            data = b"".join(response.streaming_content)
        finally:
            response.close()
        self.assertEqual(hashlib.sha256(data).hexdigest(), part["sha256"])
        self.assertIn(part["filename"], response["Content-Disposition"])
        path = self.root / receipt["id"] / part["filename"]
        path.write_bytes(data + b"corrupted")
        self.assertEqual(self.client.get(endpoint).status_code, 503)

    def test_global_metadata_and_parts_are_not_available_to_a_business_owner_or_anonymous(self):
        receipt = self.prepare()
        endpoints = ("/api/v1/backups/", f"/api/v1/backups/{receipt['id']}/",
            f"/api/v1/backups/{receipt['id']}/parts/1/")
        self.authenticate(self.owner)
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.client.get(endpoint).status_code, 403)
        self.assertEqual(self.client.post(endpoints[0], self.payload(), format="json").status_code, 403)
        self.client.credentials()
        self.assertEqual(self.client.get(endpoints[0]).status_code, 401)

    def test_missing_key_is_a_sanitized_failure_and_creates_no_job(self):
        with override_settings(BACKUP_ENCRYPTION_KEY=""):
            response = self.client.post("/api/v1/backups/", self.payload(), format="json")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(BackupJob.objects.exists())

    def test_backup_key_must_be_distinct_from_django_and_smtp_credentials(self):
        for field in ("SECRET_KEY", "EMAIL_HOST_PASSWORD"):
            with self.subTest(field=field), override_settings(**{field: self.key.decode()}):
                response = self.client.post("/api/v1/backups/", self.payload(), format="json")
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(self.key.decode(), response.content.decode())
        self.assertFalse(BackupJob.objects.exists())

    def test_generation_failure_is_recorded_without_secrets_or_plaintext_files(self):
        with patch("backups.services.jobs.build_archive", side_effect=OSError("private-secret-content")):
            response = self.client.post("/api/v1/backups/", self.payload(), format="json")
        self.assertEqual(response.status_code, 503)
        job = BackupJob.objects.get()
        self.assertEqual(job.state, "failed")
        self.assertTrue(job.failure_code)
        self.assertFalse(BackupPart.objects.exists())
        self.assertNotIn("private-secret-content", json.dumps(list(BackupEvent.objects.values("details"))))
        self.assertNotIn("private-secret-content", response.content.decode())
        self.assertFalse(list(self.root.rglob("*.sqlite3")))

    def test_preparing_job_blocks_a_second_snapshot_without_overwriting_it(self):
        first = BackupJob.objects.create(operation_id=uuid.uuid4(), requested_by=self.technical,
            recipient="backup@example.test", reason="Déjà en cours", key_id="test-primary")
        response = self.client.post("/api/v1/backups/", self.payload(), format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(BackupJob.objects.get().id, first.id)

    def test_verified_parts_survive_a_metadata_write_failure_for_explicit_recovery(self):
        with patch.object(BackupPart.objects, "bulk_create", side_effect=OperationalError("metadata storage unavailable")):
            response = self.client.post("/api/v1/backups/", self.payload(), format="json")
        self.assertEqual(response.status_code, 503)
        job = BackupJob.objects.get()
        self.assertEqual(job.state, "failed")
        paths = list((self.root / str(job.id)).glob("*.axbkp"))
        self.assertGreater(len(paths), 0)
        self.assertTrue((self.root / str(job.id) / "manifest.json").is_file())
        from backups.services.archive import verify_bundle
        result = verify_bundle(paths, self.key, Path(self.directory.name) / "recovered")
        self.assertEqual(result.manifest["backup_id"], str(job.id))
        self.assertFalse(BackupPart.objects.exists())

    def test_role_and_session_removed_after_authentication_block_preparation(self):
        original = DeviceJWTAuthentication.authenticate
        for mode in ("role", "session"):
            User.objects.filter(id=self.technical.id).update(is_superuser=True)
            self.session = self.authenticate(self.technical)

            def removed(authenticator, request):
                result = original(authenticator, request)
                if mode == "role":
                    User.objects.filter(id=self.technical.id).update(is_superuser=False)
                else:
                    DeviceSession.objects.filter(id=request.device_session.id).update(revoked_at=timezone.now())
                return result

            with self.subTest(mode=mode), patch.object(DeviceJWTAuthentication, "authenticate", autospec=True, side_effect=removed):
                response = self.client.post("/api/v1/backups/", self.payload(), format="json")
                self.assertEqual(response.status_code, 403 if mode == "role" else 401)
        self.assertFalse(BackupJob.objects.exists())
