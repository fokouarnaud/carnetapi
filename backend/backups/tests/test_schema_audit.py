"""Schema identity ignores formatting and data, while retaining every SQL rule."""
from contextlib import closing
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import SimpleTestCase


class SchemaProfileTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="restore-schema-profile-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def database(self, name, ddl):
        path = self.root / name
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(ddl)
            connection.commit()
        return path

    def profile(self, path):
        from backups.services.schema_audit import schema_profile
        return schema_profile(path)

    def test_formatting_comments_and_record_values_do_not_change_schema_identity(self):
        first = self.database("first.sqlite3", "CREATE TABLE item (id INTEGER PRIMARY KEY, price INTEGER NOT NULL CHECK (price >= 0), note TEXT DEFAULT 'Low Stock'); INSERT INTO item VALUES (1, 500, 'first');")
        second = self.database("second.sqlite3", "create table item (id integer primary key, /* formatting */ price integer not null check(price>=0), note text default 'Low Stock'); INSERT INTO item VALUES (8, 900, 'second');")
        self.assertEqual(self.profile(first), self.profile(second))

    def test_changed_check_or_literal_default_changes_schema_identity(self):
        first = self.database("first.sqlite3", "CREATE TABLE item (price INTEGER CHECK (price >= 0), note TEXT DEFAULT 'Low Stock');")
        check = self.database("check.sqlite3", "CREATE TABLE item (price INTEGER CHECK (price >= -1), note TEXT DEFAULT 'Low Stock');")
        literal = self.database("literal.sqlite3", "CREATE TABLE item (price INTEGER CHECK (price >= 0), note TEXT DEFAULT 'low stock');")
        self.assertNotEqual(self.profile(first), self.profile(check))
        self.assertNotEqual(self.profile(first), self.profile(literal))

    def test_autoindexes_are_included_but_internal_statistics_and_sequence_values_are_not(self):
        path = self.database("indexed.sqlite3", "CREATE TABLE item (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE); INSERT INTO item(code) VALUES ('first');")
        before = self.profile(path)
        self.assertTrue(any(row["name"].startswith("sqlite_autoindex_") for row in before["objects"]))
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("INSERT INTO item(code) VALUES ('second')")
            connection.execute("ANALYZE")
            connection.commit()
        self.assertEqual(self.profile(path), before)

    def test_missing_or_corrupt_source_is_refused_without_creating_a_database(self):
        from backups.services.schema_audit import SchemaAuditError
        absent = self.root / "absent.sqlite3"
        with self.assertRaisesMessage(SchemaAuditError, "restore_schema_unreadable"):
            self.profile(absent)
        self.assertFalse(absent.exists())
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"not a SQLite database")
        with self.assertRaisesMessage(SchemaAuditError, "restore_schema_unreadable"):
            self.profile(corrupt)

    def test_reference_rejects_missing_stale_or_corrupt_metadata_and_checks_actual_rules(self):
        from backups.services.schema_audit import audit_schema, SchemaAuditError
        source = self.database("source.sqlite3", "CREATE TABLE item (price INTEGER CHECK(price>=0));")
        reference = self.root / "reference.json"
        metadata = {"runtime": {"sqlite": "trusted"}, "migration_code_sha256": "a"*64}
        with self.assertRaisesMessage(SchemaAuditError, "restore_schema_reference_missing"):
            audit_schema(source, reference, metadata)
        artifact = {"format": "app-x-sqlite-schema-reference", "format_version": 1, **metadata, "schema": self.profile(source)}
        reference.write_text(json.dumps(artifact), encoding="utf-8")
        accepted = audit_schema(source, reference, metadata)
        self.assertTrue(accepted["schema_audit_checked"])
        with self.assertRaisesMessage(SchemaAuditError, "restore_schema_reference_stale"):
            audit_schema(source, reference, {**metadata, "migration_code_sha256": "b"*64})
        altered = self.database("altered.sqlite3", "CREATE TABLE item (price INTEGER CHECK(price>=-1));")
        with self.assertRaisesMessage(SchemaAuditError, "restore_schema_mismatch"):
            audit_schema(altered, reference, metadata)
        artifact["schema"]["sha256"] = "0"*64
        reference.write_text(json.dumps(artifact), encoding="utf-8")
        with self.assertRaisesMessage(SchemaAuditError, "restore_schema_reference_invalid"):
            audit_schema(source, reference, metadata)

    def test_metadata_reads_are_bounded_and_literal_comment_markers_remain_significant(self):
        from backups.services import schema_audit
        source = self.database("bounded.sqlite3", "CREATE TABLE item (note TEXT DEFAULT '/* literal */');")
        different = self.database("different.sqlite3", "CREATE TABLE item (note TEXT DEFAULT '/* changed */');")
        self.assertNotEqual(self.profile(source), self.profile(different))
        with patch.object(schema_audit, "MAX_SQL_BYTES", 16), self.assertRaisesMessage(schema_audit.SchemaAuditError, "restore_schema_unreadable"):
            self.profile(source)

    def test_unexpected_trigger_cannot_hide_behind_an_internal_sqlite_prefix(self):
        source = self.database("hidden-trigger.sqlite3", "CREATE TABLE item (id INTEGER PRIMARY KEY);")
        before = self.profile(source)
        with closing(sqlite3.connect(source)) as connection:
            connection.execute("CREATE TRIGGER ordinary_trigger AFTER INSERT ON item BEGIN SELECT 1; END")
            sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='ordinary_trigger'").fetchone()[0]
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute("UPDATE sqlite_master SET name=?,sql=? WHERE type='trigger' AND name='ordinary_trigger'",
                ("sqlite_hidden_restore_trigger", sql.replace("ordinary_trigger", "sqlite_hidden_restore_trigger")))
            connection.execute("PRAGMA writable_schema=OFF")
            connection.commit()
        after = self.profile(source)
        self.assertNotEqual(after, before)
        self.assertTrue(any(row["type"] == "trigger" and row["name"] == "sqlite_hidden_restore_trigger" for row in after["objects"]))


class SchemaReferenceCommandTests(SimpleTestCase):
    def test_reference_rebuild_matches_bundle_without_loading_application_settings_or_live_database(self):
        repository = Path(__file__).resolve().parents[3]
        with TemporaryDirectory(prefix="schema-reference-check-") as directory:
            root = Path(directory)
            copy = root / "reference.json"
            shutil.copyfile(repository / "backend/restore_schema.json", copy)
            before = copy.read_bytes()
            trap = root / "live-must-not-exist.sqlite3"
            completed = subprocess.run([sys.executable, "-I", "-X", "utf8", str(repository / "tools/restore_schema_manifest.py"),
                "--output", str(copy), "--check"], env={**os.environ, "DJANGO_SETTINGS_MODULE": "broken_settings",
                    "APP_DB_PATH": str(trap)}, text=True, capture_output=True, timeout=150)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), {"schema_reference": "current"})
            self.assertEqual(copy.read_bytes(), before)
            self.assertFalse(trap.exists())

    def test_check_mismatch_is_non_mutating_and_worker_environment_contains_no_application_secret(self):
        repository = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location("restore_schema_manifest_test", repository / "tools/restore_schema_manifest.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with TemporaryDirectory(prefix="schema-reference-stale-") as directory:
            output = Path(directory) / "reference.json"
            output.write_bytes(b"preserve this older reference")
            def generated(arguments, **options):
                self.assertNotIn("APP_DB_PATH", options["env"])
                self.assertNotIn("APP_EMAIL_HOST_PASSWORD", options["env"])
                self.assertNotIn("APP_BACKUP_ENCRYPTION_KEY", options["env"])
                work = Path(options["cwd"])
                shutil.copyfile(repository / "backend/restore_schema.json", work / "worker_report.json")
                return subprocess.CompletedProcess(arguments, 0)
            with patch.dict(os.environ, {"APP_DB_PATH": "test-only-live.sqlite3", "APP_EMAIL_HOST_PASSWORD": "test-only-mail-secret",
                    "APP_BACKUP_ENCRYPTION_KEY": "test-only-backup-secret"}),\
                    patch.object(module.subprocess, "run", side_effect=generated), patch("sys.stderr", io.StringIO()):
                self.assertEqual(module.main(["--output", str(output), "--check"]), 1)
            self.assertEqual(output.read_bytes(), b"preserve this older reference")
