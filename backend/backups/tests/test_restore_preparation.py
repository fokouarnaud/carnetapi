"""US-43: migrations execute on an authenticated disposable copy, never live data."""
from contextlib import closing
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
import uuid
from cryptography.fernet import Fernet
from django.test import SimpleTestCase
from backups.services.archive import build_archive


class RestorePreparationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fixture_directory = TemporaryDirectory(prefix="restore-source-fixture-")
        cls.addClassCleanup(cls.fixture_directory.cleanup)
        cls.fixture_root = Path(cls.fixture_directory.name)
        cls.source = cls.fixture_root / "old.sqlite3"
        cls.repository = Path(__file__).resolve().parents[3]
        builder = '''
import os,sys,json
from io import BytesIO
sys.path.insert(0,sys.argv[1])
os.environ['DJANGO_SETTINGS_MODULE']='config.test_settings'
os.environ['APP_DB_PATH']=sys.argv[2]
import django
django.setup()
from django.core.management import call_command
if len(sys.argv)>3 and sys.argv[3]=='legacy':
 call_command('migrate','core','0080_alter_membership_options_alter_shop_options_and_more',verbosity=0,interactive=False)
else:
 call_command('migrate',verbosity=0,interactive=False)
 call_command('migrate','backups','0011_backupschedulerstate_backupschedulerun',verbosity=0,interactive=False)
from identity.models import User,Shop
from catalogue.models import Product,ProductPhoto
from PIL import Image
owner=User.objects.create_user(username='source-owner')
shop=Shop.objects.create(owner=owner,name='Boutique historique')
product=Product.objects.create(shop=shop,name='Produit conservé',unit_kind='piece',unit_label='pièce',sale_price_fcfa=500)
photo=BytesIO()
Image.new('RGB',(16,16),'blue').save(photo,format='JPEG')
ProductPhoto.objects.create(product=product,data=photo.getvalue(),width=16,height=16)
print(json.dumps({'shop':str(shop.id),'product':str(product.id)}))
'''
        cls.builder = builder
        completed = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", builder,
            str(cls.repository / "backend"), str(cls.source)], capture_output=True, text=True, timeout=120)
        if completed.returncode:
            raise AssertionError(completed.stderr)
        cls.identities = json.loads(completed.stdout)

    def setUp(self):
        self.directory = TemporaryDirectory(prefix="restore-preparation-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.key = Fernet.generate_key()
        self.archive = build_archive(self.source, self.root / "parts", self.key, backup_id=uuid.uuid4(), key_id="test", max_part_bytes=32768)
        self.paths = [self.root / "parts" / row["filename"] for row in self.archive.parts]
        self.expected = json.loads((self.repository / "backend/backup_schema.json").read_text(encoding="utf-8"))

    def prepare(self, **values):
        from backups.services.restore_preparation import prepare_restore
        return prepare_restore(self.paths, self.key, values.pop("output_dir", self.root / "prepared"),
            expected_migrations=self.expected, **values)

    def test_preparation_result_migrates_copy_preserves_data_and_cannot_claim_activation_ready(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        encrypted_before = [path.read_bytes() for path in self.paths]
        result = self.prepare()
        self.assertTrue(result.path.is_file())
        self.assertFalse(result.manifest["activation_allowed"])
        self.assertFalse(result.manifest["permissions_revalidated"])
        self.assertFalse(result.manifest["live_database_replaced"])
        self.assertTrue(result.manifest["migration_execution_checked"])
        self.assertTrue(result.manifest["schema_audit_checked"])
        self.assertEqual(len(result.manifest["schema_sha256"]), 64)
        self.assertIn(["backups", "0012_backupschedulerun_failure_code"], result.manifest["applied_migrations"])
        self.assertEqual(result.manifest["source_snapshot_sha256"], before)
        self.assertEqual(result.manifest["candidate_sha256"], hashlib.sha256(result.path.read_bytes()).hexdigest())
        with closing(sqlite3.connect(self.source)) as source:
            source_photo = source.execute("SELECT data FROM core_productphoto WHERE product_id=?", (uuid.UUID(self.identities["product"]).hex,)).fetchone()[0]
        with closing(sqlite3.connect(result.path)) as candidate:
            self.assertIn("failure_code", [row[1] for row in candidate.execute("PRAGMA table_info(backups_backupschedulerun)")])
            self.assertEqual(candidate.execute("SELECT name FROM core_shop WHERE id=?", (uuid.UUID(self.identities["shop"]).hex,)).fetchone()[0], "Boutique historique")
            self.assertEqual(candidate.execute("SELECT data FROM core_productphoto WHERE product_id=?", (uuid.UUID(self.identities["product"]).hex,)).fetchone()[0], source_photo)
            self.assertEqual(candidate.execute("SELECT sale_price_fcfa FROM core_product WHERE id=?", (uuid.UUID(self.identities["product"]).hex,)).fetchone()[0], 500)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        self.assertEqual([path.read_bytes() for path in self.paths], encrypted_before)
        saved = json.loads((result.path.parent / "prepare_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, result.manifest)

    def test_wrong_key_stops_before_worker_and_never_publishes_plaintext(self):
        from backups.services.archive import BackupArchiveError
        from backups.services.restore_preparation import prepare_restore
        with patch("subprocess.run") as worker, self.assertRaises(BackupArchiveError):
            prepare_restore(self.paths, Fernet.generate_key(), self.root / "rejected", expected_migrations=self.expected)
        worker.assert_not_called()
        self.assertFalse(list((self.root / "rejected").rglob("*.sqlite3")))

    def test_existing_output_is_refused_without_overwriting_its_contents(self):
        from backups.services.restore_preparation import RestorePreparationError
        output = self.root / "existing"
        output.mkdir()
        marker = output / "candidate.sqlite3"
        marker.write_bytes(b"existing data must remain")
        with self.assertRaisesMessage(RestorePreparationError, "restore_output_exists"):
            self.prepare(output_dir=output)
        self.assertEqual(marker.read_bytes(), b"existing data must remain")

    def test_console_works_with_broken_application_settings_and_ignores_live_database_environment(self):
        keyfile = self.root / "key"
        keyfile.write_bytes(self.key)
        trap = self.root / "live-should-not-be-created.sqlite3"
        environment = {**os.environ, "DJANGO_SETTINGS_MODULE": "broken_application_settings",
            "APP_DB_PATH": str(trap), "APP_EMAIL_HOST_PASSWORD": "test-only-live-smtp-secret"}
        completed = subprocess.run([sys.executable, "-I", "-X", "utf8", str(self.repository / "tools/prepare_server_restore.py"),
            "--key-file", str(keyfile), "--expected-migrations", str(self.repository / "backend/backup_schema.json"),
            "--output-dir", str(self.root / "console"), *map(str, self.paths)],
            env=environment, capture_output=True, text=True, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["migration_execution_checked"])
        self.assertFalse(report["live_database_replaced"])
        self.assertFalse(report["activation_allowed"])
        self.assertFalse(trap.exists())
        self.assertNotIn("test-only-live-smtp-secret", completed.stdout+completed.stderr)

    def archive_modified_copy(self, statement):
        damaged = self.root / "damaged.sqlite3"
        shutil.copyfile(self.source, damaged)
        with closing(sqlite3.connect(damaged)) as database:
            if callable(statement):
                statement(database)
            else:
                database.execute(statement)
            database.commit()
        archive = build_archive(damaged, self.root / "damaged-parts", self.key, backup_id=uuid.uuid4(), key_id="test", max_part_bytes=32768)
        self.paths = [self.root / "damaged-parts" / row["filename"] for row in archive.parts]

    def test_inconsistent_applied_history_is_rejected_without_publishing_a_candidate(self):
        from backups.services.restore_preparation import RestorePreparationError
        self.archive_modified_copy("DELETE FROM django_migrations WHERE app='backups' AND name='0009_backupdispatch_sendattempt_dispatch_and_more'")
        with self.assertRaisesMessage(RestorePreparationError, "restore_migration_history_invalid"):
            self.prepare()
        self.assertFalse(list((self.root / "prepared").rglob("*.sqlite3")))

    def test_missing_table_is_rejected_even_if_migration_identifiers_are_known(self):
        from backups.services.restore_preparation import RestorePreparationError
        self.archive_modified_copy("DROP TABLE django_session")
        with self.assertRaisesMessage(RestorePreparationError, "restore_schema_incomplete"):
            self.prepare()
        self.assertFalse(list((self.root / "prepared").rglob("*.sqlite3")))

    def test_worker_timeout_does_not_leave_a_plaintext_candidate_or_receipt(self):
        from backups.services.restore_preparation import RestorePreparationError
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("isolated-test-worker", 1)),\
                self.assertRaisesMessage(RestorePreparationError, "restore_migration_timeout"):
            self.prepare()
        self.assertFalse(list((self.root / "prepared").rglob("*.sqlite3")))
        self.assertFalse((self.root / "prepared/prepare_manifest.json").exists())

    def test_stale_runtime_inventory_is_rejected_before_migration_execution(self):
        from backups.services.restore_preparation import RestorePreparationError, prepare_restore
        expected = [row for row in self.expected if row != ["backups", "0012_backupschedulerun_failure_code"]]
        with self.assertRaisesMessage(RestorePreparationError, "restore_runtime_schema_mismatch"):
            prepare_restore(self.paths, self.key, self.root / "stale-runtime", expected_migrations=expected)
        self.assertFalse(list((self.root / "stale-runtime").rglob("*.sqlite3")))

    def test_publication_collision_keeps_the_other_receipt_and_removes_our_candidate(self):
        from backups.services import restore_preparation
        original = restore_preparation._write_json
        marker = self.root / "prepared/prepare_manifest.json"
        def collided(path, value, *args, **kwargs):
            if path.name == "prepare_manifest.json":
                path.write_bytes(b"another process receipt")
            return original(path, value, *args, **kwargs)
        with patch.object(restore_preparation, "_write_json", side_effect=collided), self.assertRaises(FileExistsError):
            self.prepare()
        self.assertTrue(marker.exists(), "A failed preparation must preserve a receipt it did not create")
        self.assertEqual(marker.read_bytes(), b"another process receipt")
        self.assertFalse((self.root / "prepared/candidate.sqlite3").exists())

    def test_legacy_core_database_migrates_into_domain_apps_without_changing_business_identity(self):
        legacy = self.root / "legacy-core.sqlite3"
        built = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", self.builder,
            str(self.repository / "backend"), str(legacy), "legacy"], capture_output=True, text=True, timeout=120)
        self.assertEqual(built.returncode, 0, built.stderr)
        identities = json.loads(built.stdout)
        original_digest = hashlib.sha256(legacy.read_bytes()).hexdigest()
        archive = build_archive(legacy, self.root / "legacy-parts", self.key, backup_id=uuid.uuid4(), key_id="test", max_part_bytes=32768)
        self.paths = [self.root / "legacy-parts" / row["filename"] for row in archive.parts]
        result = self.prepare()
        self.assertIn(["core", "0081_domain_apps"], result.manifest["applied_migrations"])
        with closing(sqlite3.connect(result.path)) as candidate:
            self.assertEqual(candidate.execute("SELECT name FROM core_shop WHERE id=?", (uuid.UUID(identities["shop"]).hex,)).fetchone()[0], "Boutique historique")
            self.assertEqual(candidate.execute("SELECT COUNT(*) FROM django_content_type WHERE app_label='identity' AND model='shop'").fetchone()[0], 1)
            self.assertEqual(candidate.execute("SELECT COUNT(*) FROM django_content_type WHERE app_label='core' AND model='shop'").fetchone()[0], 0)
        self.assertEqual(hashlib.sha256(legacy.read_bytes()).hexdigest(), original_digest)

    def test_removed_check_constraint_is_rejected_even_when_data_and_history_are_valid(self):
        from backups.services.restore_preparation import RestorePreparationError
        def removed_check(database):
            original = database.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='core_shop'").fetchone()[0]
            altered, count = re.subn(r',\s*CONSTRAINT\s+"shop_discount_cap_range"\s+CHECK\s*\([^)]*\)', "", original)
            self.assertEqual(count, 1, "The fixture must remove exactly the real discount-cap constraint")
            database.execute("PRAGMA writable_schema=ON")
            database.execute("UPDATE sqlite_master SET sql=? WHERE type='table' AND name='core_shop'", (altered,))
            database.execute("PRAGMA writable_schema=OFF")
        self.archive_modified_copy(removed_check)
        with self.assertRaisesMessage(RestorePreparationError, "restore_schema_mismatch"):
            self.prepare()
        self.assertFalse(list((self.root / "prepared").rglob("*.sqlite3")))

    def test_missing_product_index_is_rejected_without_activating_a_candidate(self):
        from backups.services.restore_preparation import RestorePreparationError
        def drop_index(database):
            name = database.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='core_product' AND sql LIKE 'CREATE INDEX%' ORDER BY name LIMIT 1").fetchone()[0]
            database.execute('DROP INDEX "' + name.replace('"', '""') + '"')
        self.archive_modified_copy(drop_index)
        with self.assertRaisesMessage(RestorePreparationError, "restore_schema_mismatch"):
            self.prepare()
        self.assertFalse(list((self.root / "prepared").rglob("*.sqlite3")))

    def test_unexpected_table_view_or_trigger_is_rejected(self):
        from backups.services.restore_preparation import RestorePreparationError
        variants = {
            "table": "CREATE TABLE unexpected_restore_table (id INTEGER PRIMARY KEY)",
            "view": "CREATE VIEW unexpected_restore_view AS SELECT id FROM core_product",
            "trigger": "CREATE TRIGGER unexpected_restore_trigger AFTER INSERT ON core_product BEGIN SELECT 1; END",
        }
        for name, statement in variants.items():
            with self.subTest(object_type=name):
                self.archive_modified_copy(statement)
                output = self.root / f"rejected-{name}"
                with self.assertRaisesMessage(RestorePreparationError, "restore_schema_mismatch"):
                    self.prepare(output_dir=output)
                self.assertFalse(list(output.rglob("*.sqlite3")))
