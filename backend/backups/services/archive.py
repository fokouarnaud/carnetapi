"""Versioned, bounded encrypted parts; verification never replaces a live database.

Each part contains a binary-encoded standard Fernet token. Its authenticated
plaintext binds the archive UUID, complete digests, total count and part number.
This module intentionally has no Django dependency for emergency verification.
"""
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from contextlib import closing
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import uuid

from cryptography.fernet import Fernet, InvalidToken

from common.sqlite_checks import integer_balance_matches
from stock.services.cost_constraints import register_cost_functions


MAGIC = b"APPXBKP1"
MAX_PART_BYTES = 15000000
MAX_HEADER_BYTES = 65536
MAX_SNAPSHOT_BYTES = 500000000
FORMAT = "app-x-server-backup"


class BackupArchiveError(ValueError):
    """A stable diagnostic, without key material or untrusted archive content."""


@dataclass(frozen=True)
class BuiltArchive:
    manifest: dict
    parts: list


@dataclass(frozen=True)
class VerifiedSnapshot:
    path: Path
    manifest: dict


def _digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(65536), b""):
            result.update(block)
    return result.hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _cipher(key):
    try:
        return Fernet(key)
    except (ValueError, TypeError):
        raise BackupArchiveError("invalid_backup_key") from None


def validate_snapshot(path):
    """Check database pages, all constraints and foreign keys, without Django."""
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as database:
            register_cost_functions(None, SimpleNamespace(vendor="sqlite", connection=database))
            database.create_function("integer_balance_matches", 4, integer_balance_matches, deterministic=True)
            if database.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BackupArchiveError("snapshot_integrity_failed")
            if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupArchiveError("snapshot_foreign_keys_failed")
            if database.execute("SELECT 1 FROM django_migrations WHERE typeof(app) != 'text' OR typeof(name) != 'text' OR length(app) > 255 OR length(name) > 255 LIMIT 1").fetchone():
                raise BackupArchiveError("invalid_snapshot_schema")
            migrations = [list(row) for row in database.execute("SELECT app, name FROM django_migrations ORDER BY app, name LIMIT 2049")]
            if not migrations:
                raise BackupArchiveError("snapshot_schema_missing")
            if len(migrations) > 2048:
                raise BackupArchiveError("snapshot_schema_limit")
            return migrations
    except sqlite3.Error:
        raise BackupArchiveError("invalid_sqlite_snapshot") from None


def build_archive(snapshot, output_dir, key, *, backup_id, key_id, created_at=None, max_part_bytes=MAX_PART_BYTES):
    cipher = _cipher(key)
    backup_id = str(uuid.UUID(str(backup_id)))
    if not isinstance(key_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key_id):
        raise BackupArchiveError("invalid_key_identifier")
    if type(max_part_bytes) is not int or not 1024 <= max_part_bytes <= MAX_PART_BYTES:
        raise BackupArchiveError("invalid_part_limit")
    snapshot = Path(snapshot)
    size = snapshot.stat().st_size
    if not 0 < size <= MAX_SNAPSHOT_BYTES:
        raise BackupArchiveError("snapshot_size_limit")
    migrations = validate_snapshot(snapshot)
    instant = created_at or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise BackupArchiveError("backup_time_requires_timezone")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    created_files = []
    try:
        with TemporaryDirectory(prefix="compressed-", dir=output_dir) as temporary:
            compressed = Path(temporary) / "snapshot.gz"
            with snapshot.open("rb") as source, compressed.open("wb") as target:
                with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as zipped:
                    shutil.copyfileobj(source, zipped, length=65536)
            common = {"format": FORMAT, "format_version": 1, "backup_id": backup_id, "key_id": key_id,
                "created_at": instant.astimezone(timezone.utc).isoformat(), "snapshot_bytes": size,
                "snapshot_sha256": _digest(snapshot), "compressed_bytes": compressed.stat().st_size,
                "compressed_sha256": _digest(compressed), "migrations": migrations}
            largest = _json({**common, "number": 2147483647, "total_parts": 2147483647})
            if len(largest) > MAX_HEADER_BYTES:
                raise BackupArchiveError("backup_header_limit")
            # Fernet binary overhead: 57 bytes plus at most 16 padding bytes.
            chunk_size = max_part_bytes - len(MAGIC) - 57 - 16 - 4 - len(largest)
            if chunk_size <= 0:
                raise BackupArchiveError("part_limit_too_small")
            total = math.ceil(common["compressed_bytes"] / chunk_size)
            if total > 32768:
                raise BackupArchiveError("backup_part_count_limit")
            parts = []
            with compressed.open("rb") as source:
                for number in range(1, total + 1):
                    header = _json({**common, "number": number, "total_parts": total})
                    plaintext = struct.pack(">I", len(header)) + header + source.read(chunk_size)
                    encoded = cipher.encrypt(plaintext)
                    data = MAGIC + base64.urlsafe_b64decode(encoded)
                    if len(data) > max_part_bytes:
                        raise BackupArchiveError("encrypted_part_limit")
                    filename = f"backup-{backup_id}.part-{number:04d}-of-{total:04d}.axbkp"
                    path = output_dir / filename
                    with path.open("xb") as target:
                        created_files.append(path)
                        target.write(data)
                        target.flush()
                        os.fsync(target.fileno())
                    parts.append({"number": number, "filename": filename, "size_bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
            return BuiltArchive({**common, "total_parts": total, "parts": parts}, parts)
    except BaseException:
        for path in created_files:
            path.unlink(missing_ok=True)
        raise


def _part_bytes(path):
    with Path(path).open("rb") as source:
        data = source.read(MAX_PART_BYTES + 1)
    if not 0 < len(data) <= MAX_PART_BYTES:
        raise BackupArchiveError("encrypted_part_limit")
    return data


def _read_part(path, cipher):
    return _decode_part(_part_bytes(path), cipher)


def _decode_part(data, cipher):
    if not data.startswith(MAGIC):
        raise BackupArchiveError("unsupported_backup_format")
    try:
        plaintext = cipher.decrypt(base64.urlsafe_b64encode(data[len(MAGIC):]))
    except InvalidToken:
        raise BackupArchiveError("backup_authentication_failed") from None
    if len(plaintext) < 4:
        raise BackupArchiveError("invalid_backup_header")
    length = struct.unpack(">I", plaintext[:4])[0]
    if not 0 < length <= MAX_HEADER_BYTES or 4 + length > len(plaintext):
        raise BackupArchiveError("invalid_backup_header")
    try:
        header = json.loads(plaintext[4:4 + length])
    except (ValueError, UnicodeError, RecursionError):
        raise BackupArchiveError("invalid_backup_header") from None
    required = {"format", "format_version", "backup_id", "key_id", "created_at", "snapshot_bytes",
        "snapshot_sha256", "compressed_bytes", "compressed_sha256", "migrations", "number", "total_parts"}
    if not isinstance(header, dict) or set(header) != required or header["format"] != FORMAT or type(header["format_version"]) is not int or header["format_version"] != 1:
        raise BackupArchiveError("unsupported_backup_format")
    try:
        if str(uuid.UUID(header["backup_id"])) != header["backup_id"]:
            raise ValueError()
        instant = datetime.fromisoformat(header["created_at"])
        if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise BackupArchiveError("invalid_backup_header") from None
    for field in ("snapshot_sha256", "compressed_sha256"):
        if not isinstance(header[field], str) or not re.fullmatch("[0-9a-f]{64}", header[field]):
            raise BackupArchiveError("invalid_backup_header")
    for field in ("number", "total_parts", "snapshot_bytes", "compressed_bytes"):
        if type(header[field]) is not int or header[field] <= 0:
            raise BackupArchiveError("invalid_backup_header")
    if header["number"] > header["total_parts"] or header["total_parts"] > 32768 or header["snapshot_bytes"] > MAX_SNAPSHOT_BYTES or header["compressed_bytes"] > MAX_SNAPSHOT_BYTES + 1000000:
        raise BackupArchiveError("backup_size_limit")
    migrations = header["migrations"]
    if not isinstance(migrations, list) or len(migrations) > 2048 or any(
            not isinstance(row, list) or len(row) != 2 or any(not isinstance(item, str) or not 0 < len(item) <= 255 for item in row)
            for row in migrations):
        raise BackupArchiveError("invalid_backup_header")
    if not isinstance(header["key_id"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", header["key_id"]):
        raise BackupArchiveError("invalid_backup_header")
    return header, plaintext[4 + length:]


@dataclass(frozen=True)
class PartInspection:
    header: dict
    sha256: str
    size_bytes: int


def inspect_part(path, key):
    """Authenticate one bounded part; expose metadata only, never a verified snapshot."""
    data = _part_bytes(path)
    header, _block = _decode_part(data, _cipher(key))
    return PartInspection(header, hashlib.sha256(data).hexdigest(), len(data))


def verify_bundle(paths, key, output_dir, *, expected_migrations=None):
    """Authenticate all parts and validate a snapshot before exposing any file."""
    cipher = _cipher(key)
    paths = list(paths)
    if not paths or len(paths) > 32768:
        raise BackupArchiveError("backup_parts_missing")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = None
    copied = False
    try:
        with TemporaryDirectory(prefix="verified-", dir=output_dir) as temporary:
            compressed = Path(temporary) / "snapshot.gz"
            common = None
            seen = set()
            # Retain authenticated part order, not caller-controlled filenames.
            headers = []
            for path in paths:
                header, _block = _read_part(path, cipher)
                base = {key: value for key, value in header.items() if key not in {"number"}}
                if common is None:
                    common = base
                if base != common or header["number"] in seen:
                    raise BackupArchiveError("backup_parts_mismatch")
                seen.add(header["number"])
                headers.append((header["number"], path))
            if len(paths) != common["total_parts"] or seen != set(range(1, common["total_parts"] + 1)):
                raise BackupArchiveError("backup_parts_missing")
            if expected_migrations is not None and not {tuple(row) for row in common["migrations"]}.issubset({tuple(row) for row in expected_migrations}):
                raise BackupArchiveError("backup_schema_incompatible")
            size = 0
            with compressed.open("xb") as target:
                for _number, path in sorted(headers):
                    header, block = _read_part(path, cipher)
                    if header["number"] != _number or {key: value for key, value in header.items() if key != "number"} != common:
                        raise BackupArchiveError("backup_parts_mismatch")
                    size += len(block)
                    if size > common["compressed_bytes"]:
                        raise BackupArchiveError("backup_size_mismatch")
                    target.write(block)
            if size != common["compressed_bytes"] or _digest(compressed) != common["compressed_sha256"]:
                raise BackupArchiveError("backup_digest_mismatch")
            snapshot = Path(temporary) / "snapshot.sqlite3"
            size = 0
            try:
                with gzip.open(compressed, "rb") as source, snapshot.open("xb") as target:
                    for block in iter(lambda: source.read(65536), b""):
                        size += len(block)
                        if size > common["snapshot_bytes"]:
                            raise BackupArchiveError("backup_size_mismatch")
                        target.write(block)
            except (OSError, EOFError):
                raise BackupArchiveError("invalid_backup_compression") from None
            if size != common["snapshot_bytes"] or _digest(snapshot) != common["snapshot_sha256"]:
                raise BackupArchiveError("backup_digest_mismatch")
            if validate_snapshot(snapshot) != common["migrations"]:
                raise BackupArchiveError("backup_schema_mismatch")
            final = output_dir / f"verified-{common['backup_id']}.sqlite3"
            with final.open("xb") as target, snapshot.open("rb") as source:
                copied = True
                shutil.copyfileobj(source, target, length=65536)
                target.flush()
                os.fsync(target.fileno())
            return VerifiedSnapshot(final, common)
    except BaseException:
        if copied and final is not None:
            final.unlink(missing_ok=True)
        raise
