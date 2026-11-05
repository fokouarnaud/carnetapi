"""US-43: quarantine archived credentials on a separate copy before restoration."""
from contextlib import closing
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid
from unittest.mock import patch
from django.db import connection
from django.contrib.auth.models import Group, Permission
from django.test import Client, TransactionTestCase
from django.utils import timezone
from backups.services.archive import build_archive
from backups.tests import test_import_preparation as fixtures
from identity.models import DeviceSession, Installation, Membership, ShopDeviceAccess, User


class RestoreQuarantineTests(TransactionTestCase):
    def setUp(self):
        fixtures.ImportedArchivePreparationTests.setUp(self)
        self.operator.set_password("quarantine-fixture-password")
        self.operator.save()
        self.seller = User.objects.create_user(username="quarantine-seller")
        Membership.objects.create(shop=self.product.shop, user=self.seller)
        group = Group.objects.create(name="Archived technical permissions")
        permission = Permission.objects.get(content_type__app_label="core", codename="change_user")
        group.permissions.add(permission)
        self.operator.groups.add(group)
        self.owner.user_permissions.add(permission)
        self.revoked_at = timezone.now() - timedelta(days=1)
        for user, revoked in ((self.owner, None), (self.seller, self.revoked_at)):
            installation = Installation.objects.create(user=user, revoked_at=revoked)
            session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now()+timedelta(days=30), revoked_at=revoked)
            if user == self.owner:
                from rest_framework_simplejwt.tokens import RefreshToken
                self.refresh = RefreshToken.for_user(self.owner)
                self.refresh["session_id"] = str(session.id)
                session.refresh_jti = self.refresh["jti"]
                session.save()
            ShopDeviceAccess.objects.create(shop=self.product.shop, installation=installation,
                revoked_at=revoked, verified_at=timezone.now(), offline_expires_at=timezone.now()+timedelta(days=7))
        client = Client()
        client.force_login(self.operator)
        with closing(sqlite3.connect(self.source)) as target:
            connection.connection.backup(target)
        self.archive = build_archive(self.source, self.root / "updated-parts", self.key,
            backup_id=uuid.uuid4(), key_id="quarantine-test", max_part_bytes=32768)
        self.paths = [self.root / "updated-parts" / p["filename"] for p in self.archive.parts]
        from django.db.migrations.loader import MigrationLoader
        from backups.services.restore_preparation import prepare_restore
        expected = sorted([list(key) for key in MigrationLoader(None).disk_migrations])
        self.prepared = prepare_restore(self.paths, self.key, self.root / "prepared", expected_migrations=expected)
        self.operation_id = uuid.uuid4()

    def quarantine(self, **options):
        from backups.services.restore_quarantine import quarantine_restore
        return quarantine_restore(self.prepared.path.parent, options.pop("output_dir", self.root / "quarantined"),
            operation_id=self.operation_id, requested_by=self.operator.id, reason="Contrôle des droits après panne", **options)

    def test_quarantine_blocks_archived_accounts_sessions_and_leases_without_losing_business_data(self):
        before = hashlib.sha256(self.prepared.path.read_bytes()).hexdigest()
        encrypted_before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in self.paths]
        result = self.quarantine()
        self.assertEqual(result.path.name, "candidate.sqlite3")
        self.assertNotEqual(result.path, self.prepared.path)
        self.assertEqual(result.manifest["operation_id"], str(self.operation_id))
        self.assertEqual(result.manifest["requested_by"], str(self.operator.id))
        self.assertTrue(result.manifest["access_quarantined"])
        for flag in ("activation_allowed", "permissions_revalidated", "live_database_replaced"):
            self.assertIs(result.manifest[flag], False)
        self.assertEqual(result.manifest["source_candidate_sha256"], before)
        self.assertEqual(result.manifest["candidate_sha256"], hashlib.sha256(result.path.read_bytes()).hexdigest())
        self.assertEqual(result.manifest["before"]["active_users"], 3)
        with closing(sqlite3.connect(result.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM core_user WHERE is_active=1").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM core_installation WHERE revoked_at IS NULL").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM core_devicesession WHERE revoked_at IS NULL").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM core_shopdeviceaccess WHERE revoked_at IS NULL OR verified_at IS NOT NULL OR offline_expires_at IS NOT NULL").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM django_session").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT sale_price_fcfa FROM core_product WHERE id=?", [self.product.id.hex]).fetchone()[0], 500)
            self.assertEqual(bytes(db.execute("SELECT data FROM core_productphoto WHERE product_id=?", [self.product.id.hex]).fetchone()[0]), self.photo)
            self.assertEqual(db.execute("SELECT role,is_active FROM core_membership").fetchall(), [("seller", 0)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM core_user WHERE is_superuser!=0 OR is_staff!=0").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM core_user_groups").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM core_user_user_permissions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM core_user").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT revoked_at FROM core_installation WHERE user_id=?", [self.seller.id.hex]).fetchone()[0],
                self.revoked_at.replace(tzinfo=None).isoformat(sep=" "))
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(hashlib.sha256(self.prepared.path.read_bytes()).hexdigest(), before)
        self.assertEqual(encrypted_before, [hashlib.sha256(p.read_bytes()).hexdigest() for p in self.paths])
        self.assertEqual(User.objects.filter(is_active=True).count(), 3)
        self.assertEqual(self.operator.groups.count(), 1)
        self.assertEqual(self.owner.user_permissions.count(), 1)
        self.assertEqual(json.loads((result.path.parent / "quarantine_manifest.json").read_text()), result.manifest)
        self.assertNotIn(self.key.decode(), json.dumps(result.manifest))

    def test_sqlite_failure_has_a_stable_code_and_leaves_no_published_copy(self):
        from backups.services.restore_quarantine import RestoreQuarantineError
        before = hashlib.sha256(self.prepared.path.read_bytes()).hexdigest()
        with patch("backups.services.restore_quarantine._suspend", side_effect=sqlite3.OperationalError("infrastructure detail")):
            with self.assertRaisesMessage(RestoreQuarantineError, "restore_quarantine_failed"):
                self.quarantine()
        self.assertFalse((self.root / "quarantined").exists())
        self.assertEqual(hashlib.sha256(self.prepared.path.read_bytes()).hexdigest(), before)

    def test_noncanonical_active_flags_cannot_survive_quarantine(self):
        # SQLite's BooleanField storage can contain a truthy value other than 1.
        with closing(sqlite3.connect(self.prepared.path)) as db:
            db.execute("UPDATE core_user SET is_active=2 WHERE id=?", [self.owner.id.hex])
            db.commit()
        receipt = self.prepared.path.parent / "prepare_manifest.json"
        manifest = json.loads(receipt.read_text())
        manifest.update(candidate_sha256=hashlib.sha256(self.prepared.path.read_bytes()).hexdigest(),
            candidate_bytes=self.prepared.path.stat().st_size)
        receipt.write_text(json.dumps(manifest))
        result = self.quarantine()
        with closing(sqlite3.connect(result.path)) as db:
            self.assertEqual(db.execute("SELECT is_active FROM core_user WHERE id=?", [self.owner.id.hex]).fetchone()[0], 0)
        self.assertEqual(result.manifest["before"]["active_users"], 3)

    def test_unexpected_trigger_is_refused_even_if_candidate_digest_in_receipt_is_replaced(self):
        from backups.services.restore_quarantine import RestoreQuarantineError
        with closing(sqlite3.connect(self.prepared.path)) as db:
            db.execute("CREATE TRIGGER forbid_update BEFORE UPDATE ON core_user BEGIN SELECT RAISE(ABORT,'unexpected trigger executed'); END")
            db.commit()
        receipt = self.prepared.path.parent / "prepare_manifest.json"
        manifest = json.loads(receipt.read_text())
        manifest.update(candidate_sha256=hashlib.sha256(self.prepared.path.read_bytes()).hexdigest(),
            candidate_bytes=self.prepared.path.stat().st_size)
        receipt.write_text(json.dumps(manifest))
        with patch("backups.services.restore_quarantine._suspend", side_effect=AssertionError("unexpected schema reached writes")):
            with self.assertRaisesMessage(RestoreQuarantineError, "restore_quarantine_source_invalid"):
                self.quarantine()
        self.assertFalse((self.root / "quarantined").exists())

    def test_receipt_publication_failure_removes_only_its_owned_copy(self):
        before = hashlib.sha256(self.prepared.path.read_bytes()).hexdigest()
        with patch("backups.services.restore_quarantine._write_json", side_effect=OSError("receipt write failure")):
            with self.assertRaises(OSError):
                self.quarantine()
        self.assertFalse((self.root / "quarantined").exists())
        self.assertEqual(hashlib.sha256(self.prepared.path.read_bytes()).hexdigest(), before)

    def test_real_authentication_rejects_archived_password_access_and_refresh_tokens(self):
        result = self.quarantine()
        backend = Path(__file__).resolve().parents[2]
        probe = '''
import os,sys,json
sys.path.insert(0,sys.argv[1])
os.environ['DJANGO_SETTINGS_MODULE']='config.test_settings'
os.environ['APP_DB_PATH']=sys.argv[2]
import django
django.setup()
from django.contrib.auth import authenticate
from rest_framework.test import APIClient
client=APIClient()
client.credentials(HTTP_AUTHORIZATION='Bearer '+sys.argv[3])
access=client.get('/api/v1/shops/').status_code
client.credentials()
refresh=client.post('/api/v1/auth/refresh/',{'refresh':sys.argv[4]},format='json').status_code
print(json.dumps({'password_accepted':authenticate(username='prepare-import',password='quarantine-fixture-password') is not None,'access':access,'refresh':refresh}))
'''
        for candidate, expected in ((self.prepared.path, {"password_accepted": True, "access": 200, "refresh": 200}),
                                    (result.path, {"password_accepted": False, "access": 401, "refresh": 401})):
            with self.subTest(quarantined=candidate == result.path):
                # Refresh rotates data: probe an owned copy, preserving both evidence files.
                probe_file = self.root / ("auth-quarantined.sqlite3" if candidate == result.path else "auth-prepared.sqlite3")
                import shutil
                shutil.copyfile(candidate, probe_file)
                inspected = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", probe,
                    str(backend), str(probe_file), str(self.refresh.access_token), str(self.refresh)],
                    capture_output=True, text=True, timeout=45)
                self.assertEqual(inspected.returncode, 0, inspected.stderr)
                self.assertEqual(json.loads(inspected.stdout), expected)
    def test_changed_prepared_copy_is_refused_without_creating_output_or_modifying_live_users(self):
        from backups.services.restore_quarantine import RestoreQuarantineError
        with closing(sqlite3.connect(self.prepared.path)) as db:
            db.execute("UPDATE core_product SET sale_price_fcfa=900")
            db.commit()
        with self.assertRaisesMessage(RestoreQuarantineError, "restore_quarantine_source_invalid"):
            self.quarantine()
        self.assertFalse((self.root / "quarantined").exists())
        self.assertEqual(User.objects.filter(is_active=True).count(), 3)

    def test_existing_output_is_never_overwritten_and_a_reason_is_required(self):
        from backups.services.restore_quarantine import RestoreQuarantineError
        output = self.root / "quarantined"
        output.mkdir()
        sentinel = output / "preserve.txt"
        sentinel.write_text("existing surviving files")
        with self.assertRaisesMessage(RestoreQuarantineError, "restore_output_exists"):
            self.quarantine()
        self.assertEqual(sentinel.read_text(), "existing surviving files")
        from backups.services.restore_quarantine import quarantine_restore
        with self.assertRaisesMessage(RestoreQuarantineError, "restore_quarantine_request_invalid"):
            quarantine_restore(self.prepared.path.parent, self.root / "blank", operation_id=self.operation_id,
                requested_by=self.operator.id, reason=" ")
        self.assertFalse((self.root / "blank").exists())

    def test_console_quarantine_works_without_application_settings_and_keeps_secrets_out_of_output(self):
        script = Path(__file__).resolve().parents[3] / "tools" / "quarantine_server_restore.py"
        command = [sys.executable, "-I", "-X", "utf8", str(script), str(self.prepared.path.parent),
            "--output-dir", str(self.root / "console"), "--operation-id", str(self.operation_id),
            "--requested-by", str(self.operator.id), "--reason", "Contrôle console"]
        environment = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "PATH") if name in os.environ}
        environment.update(DJANGO_SETTINGS_MODULE="unavailable_application_settings", APP_DB_PATH=str(self.source))
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertTrue(receipt["access_quarantined"])
        self.assertFalse(receipt["activation_allowed"])
        self.assertNotIn(self.key.decode(), result.stdout+result.stderr)
        self.assertEqual(User.objects.filter(is_active=True).count(), 3)
