"""Authenticated multipart files reject tampering without exposing a snapshot."""
import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import uuid

from cryptography.fernet import Fernet
from django.test import SimpleTestCase

from backups.services.archive import BackupArchiveError, MAGIC, build_archive, verify_bundle


class ArchiveFormatTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="backup-format-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.key = Fernet.generate_key()
        self.snapshot = self.root / "source.sqlite3"
        with closing(sqlite3.connect(self.snapshot)) as database:
            database.execute("CREATE TABLE django_migrations (app TEXT, name TEXT)")
            database.execute("INSERT INTO django_migrations VALUES ('sample', '0001_initial')")
            database.execute("CREATE TABLE contents (id INTEGER PRIMARY KEY, photo BLOB, label TEXT)")
            database.execute("INSERT INTO contents VALUES (1, ?, ?)", (os.urandom(90000), "Photo et historique conservés"))
            database.commit()
        self.backup_id = uuid.uuid4()
        self.archive = build_archive(self.snapshot, self.root / "parts", self.key,
            backup_id=self.backup_id, key_id="primary", max_part_bytes=32768)
        self.paths = [self.root / "parts" / row["filename"] for row in self.archive.parts]
        self.assertGreater(len(self.paths), 1)

    def test_all_parts_round_trip_in_any_input_order_with_original_bytes(self):
        result = verify_bundle(list(reversed(self.paths)), self.key, self.root / "verified")
        self.assertEqual(result.path.read_bytes(), self.snapshot.read_bytes())
        for row, path in zip(self.archive.parts, self.paths):
            self.assertEqual(row["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertLessEqual(path.stat().st_size, 32768)
            self.assertNotIn(self.key, path.read_bytes())

    def test_inspection_authenticates_metadata_without_exposing_the_plaintext_block(self):
        from dataclasses import asdict
        from backups.services.archive import inspect_part
        result = inspect_part(self.paths[1], self.key)
        self.assertEqual(set(asdict(result)), {"header", "sha256", "size_bytes"})
        self.assertEqual(result.header["number"], 2)
        self.assertEqual(result.header["backup_id"], str(self.backup_id))
        self.assertEqual(result.sha256, hashlib.sha256(self.paths[1].read_bytes()).hexdigest())
        self.assertEqual(result.size_bytes, self.paths[1].stat().st_size)
        with self.assertRaisesMessage(BackupArchiveError, "backup_authentication_failed"):
            inspect_part(self.paths[1], Fernet.generate_key())

    def test_wrong_key_corrupt_or_truncated_part_never_exposes_plaintext(self):
        for mode in ("key", "corrupt", "truncated"):
            paths = list(self.paths)
            key = Fernet.generate_key() if mode == "key" else self.key
            if mode != "key":
                broken = self.root / f"{mode}.axbkp"
                data = bytearray(paths[0].read_bytes())
                if mode == "corrupt":
                    data[len(data) // 2] ^= 1
                else:
                    data = data[:-1]
                broken.write_bytes(data)
                paths[0] = broken
            output = self.root / mode
            with self.subTest(mode=mode), self.assertRaisesMessage(BackupArchiveError, "backup_authentication_failed"):
                verify_bundle(paths, key, output)
            self.assertFalse(list(output.rglob("*.sqlite3")))

    def test_missing_repeated_and_mixed_archive_parts_are_not_complete(self):
        other = build_archive(self.snapshot, self.root / "other", self.key,
            backup_id=uuid.uuid4(), key_id="primary", max_part_bytes=32768)
        variants = {"missing": self.paths[:-1], "repeated": self.paths + [self.paths[0]],
            "mixed": [self.root / "other" / other.parts[0]["filename"], *self.paths[1:]]}
        for mode, paths in variants.items():
            output = self.root / mode
            with self.subTest(mode=mode), self.assertRaises(BackupArchiveError):
                verify_bundle(paths, self.key, output)
            self.assertFalse(list(output.rglob("*.sqlite3")))

    def test_unknown_authenticated_format_and_incompatible_schema_are_refused(self):
        original = self.paths[0].read_bytes()
        plaintext = Fernet(self.key).decrypt(base64.urlsafe_b64encode(original[len(MAGIC):]))
        length = struct.unpack(">I", plaintext[:4])[0]
        header = json.loads(plaintext[4:4 + length])
        header["format_version"] = 2
        changed = json.dumps(header).encode()
        token = Fernet(self.key).encrypt(struct.pack(">I", len(changed)) + changed + plaintext[4 + length:])
        unknown = self.root / "unknown.axbkp"
        unknown.write_bytes(MAGIC + base64.urlsafe_b64decode(token))
        with self.assertRaisesMessage(BackupArchiveError, "unsupported_backup_format"):
            verify_bundle([unknown, *self.paths[1:]], self.key, self.root / "unknown")
        with self.assertRaisesMessage(BackupArchiveError, "backup_schema_incompatible"):
            verify_bundle(self.paths, self.key, self.root / "incompatible", expected_migrations=[])

    def test_existing_archive_or_verified_file_is_never_overwritten(self):
        before = [path.read_bytes() for path in self.paths]
        with self.assertRaises(FileExistsError):
            build_archive(self.snapshot, self.root / "parts", self.key,
                backup_id=self.backup_id, key_id="primary", max_part_bytes=32768)
        self.assertEqual([path.read_bytes() for path in self.paths], before)
        verified = verify_bundle(self.paths, self.key, self.root / "verified")
        with self.assertRaises(FileExistsError):
            verify_bundle(self.paths, self.key, self.root / "verified")
        self.assertEqual(verified.path.read_bytes(), self.snapshot.read_bytes())

    def test_emergency_verification_does_not_import_django(self):
        code = '''import importlib.abc, json, sys
class NoDjango(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "django" or fullname.startswith("django."):
            raise RuntimeError("Django unavailable")
sys.meta_path.insert(0, NoDjango())
from backups.services.archive import verify_bundle
data = json.load(sys.stdin)
result = verify_bundle(data["parts"], data["key"], data["output"])
print(result.path.stat().st_size)
'''
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=Path(__file__).resolve().parents[2],
            input=json.dumps({"parts": [str(path) for path in self.paths], "key": self.key.decode(),
                "output": str(self.root / "without-django")}), text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(int(result.stdout.strip()), self.snapshot.stat().st_size)

    def test_console_verification_produces_only_a_snapshot_and_rejects_a_wrong_key(self):
        key_file = self.root / "separate.key"
        key_file.write_bytes(self.key)
        schema = self.root / "release-schema.json"
        schema.write_text(json.dumps(self.archive.manifest["migrations"]), encoding="utf-8")
        output = self.root / "console"
        output.mkdir()
        active = output / "db.sqlite3"
        active.write_bytes(b"active database must stay untouched")
        command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve().parents[3] / "tools/verify_server_backup.py"),
            "--key-file", str(key_file), "--expected-migrations", str(schema), "--output-dir", str(output),
            *[str(path) for path in self.paths]]
        result = subprocess.run(command, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertFalse(receipt["live_database_replaced"])
        self.assertFalse(receipt["migration_execution_checked"])
        self.assertEqual(Path(receipt["snapshot"]).read_bytes(), self.snapshot.read_bytes())
        self.assertEqual(active.read_bytes(), b"active database must stay untouched")
        key_file.write_bytes(Fernet.generate_key())
        result = subprocess.run(command, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stderr)["error"], "backup_authentication_failed")
        self.assertNotIn(self.key.decode(), result.stderr)
        self.assertEqual(active.read_bytes(), b"active database must stay untouched")
