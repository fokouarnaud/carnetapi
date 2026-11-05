"""Readonly schema fingerprints preserve SQL rules while ignoring formatting and data."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import string
import sqlparse
from sqlparse import tokens

MAX_OBJECTS = 4096
MAX_SQL_BYTES = 1000000
MAX_TOTAL_SQL_BYTES = 4000000


class SchemaAuditError(ValueError):
    pass


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def digest_file(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _sql_digest(sql):
    normalized = []
    for kind, value in sqlparse.lexer.tokenize(sql):
        if kind in tokens.Whitespace or kind in tokens.Comment:
            continue
        if kind in tokens.Error:
            raise SchemaAuditError("restore_schema_unreadable")
        if kind in tokens.Keyword or (kind in tokens.Name and not value.startswith(('"', "`", "["))):
            # SQLite identifiers/function names use ASCII case folding. The
            # lexer may classify CHECK differently before '(' and ' ('.
            value = " ".join(value.split()).translate(str.maketrans(string.ascii_lowercase, string.ascii_uppercase))
            normalized.append(["word", value])
            continue
        normalized.append([str(kind), value])
    return hashlib.sha256(json_bytes(normalized)).hexdigest()


def schema_profile(path):
    """Include tables, indexes (including automatic unique indexes), views and triggers.

    Row contents, B-tree page numbers, internal sequence counters and ANALYZE
    statistics do not define the application schema. SQL literal values do.
    Only known statistics tables are optional; internal-looking names do not
    hide tables, views, indexes or triggers from the audit.
    """
    objects, size = [], 0
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True)) as connection:
            rows = connection.execute("""
                SELECT type,
                    CASE WHEN length(name)<=255 THEN name END,
                    CASE WHEN length(tbl_name)<=255 THEN tbl_name END,
                    coalesce(length(sql),0),
                    CASE WHEN sql IS NULL OR length(sql)<=? THEN sql END
                FROM sqlite_master
                WHERE NOT (type='table' AND name IN ('sqlite_stat1','sqlite_stat2','sqlite_stat3','sqlite_stat4'))
                ORDER BY type,name,tbl_name LIMIT ?
            """, (MAX_SQL_BYTES, MAX_OBJECTS+1)).fetchall()
        if len(rows) > MAX_OBJECTS:
            raise SchemaAuditError("restore_schema_unreadable")
        for kind, name, table, length, sql in rows:
            if (kind not in {"table", "index", "view", "trigger"} or not isinstance(name, str)
                    or not isinstance(table, str) or length > MAX_SQL_BYTES
                    or (sql is None and not (kind == "index" and name.startswith("sqlite_autoindex_")))):
                raise SchemaAuditError("restore_schema_unreadable")
            amount = len(sql.encode("utf-8")) if sql is not None else 0
            size += amount
            if amount > MAX_SQL_BYTES or size > MAX_TOTAL_SQL_BYTES:
                raise SchemaAuditError("restore_schema_unreadable")
            objects.append({"type": kind, "name": name, "table": table,
                "sql_sha256": _sql_digest(sql) if sql is not None else None})
    except (sqlite3.Error, OSError, UnicodeError, TypeError, ValueError) as error:
        if isinstance(error, SchemaAuditError):
            raise
        raise SchemaAuditError("restore_schema_unreadable") from None
    return {"objects": objects, "sha256": hashlib.sha256(json_bytes(objects)).hexdigest()}


def audit_schema(path, reference_path, metadata):
    reference_path = Path(reference_path)
    try:
        if reference_path.stat().st_size > 1000000:
            raise SchemaAuditError("restore_schema_reference_invalid")
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SchemaAuditError("restore_schema_reference_missing") from None
    except (OSError, ValueError, UnicodeError):
        raise SchemaAuditError("restore_schema_reference_invalid") from None
    if (not isinstance(reference, dict) or reference.get("format") != "app-x-sqlite-schema-reference"
            or type(reference.get("format_version")) is not int or reference["format_version"] != 1
            or set(reference) != {"format", "format_version", "schema", *metadata}):
        raise SchemaAuditError("restore_schema_reference_invalid")
    if any(reference.get(key) != value for key, value in metadata.items()):
        raise SchemaAuditError("restore_schema_reference_stale")
    expected = reference.get("schema")
    if (not isinstance(expected, dict) or set(expected) != {"objects", "sha256"}
            or not isinstance(expected["objects"], list) or len(expected["objects"]) > MAX_OBJECTS
            or expected["sha256"] != hashlib.sha256(json_bytes(expected["objects"])).hexdigest()):
        raise SchemaAuditError("restore_schema_reference_invalid")
    if schema_profile(path) != expected:
        raise SchemaAuditError("restore_schema_mismatch")
    return {"schema_audit_checked": True, "schema_sha256": expected["sha256"],
        "schema_reference_sha256": digest_file(reference_path)}
