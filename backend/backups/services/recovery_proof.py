"""External one-use proof with an atomic effect on an attached SQLite database.

This is a console service primitive, not an authentication endpoint. Its caller
must enforce maintenance and validate the restored database before granting any
right. The external vault must never be included in a business backup.
"""
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import uuid


class RecoveryProofError(ValueError):
    """Stable diagnostic, without secrets or SQLite record values."""


class RecoveryProofConflict(RecoveryProofError):
    """An operation identity or its content cannot be reused."""


class RecoveryProofUsed(RecoveryProofError):
    """The proof was already consumed by a different operation."""


@dataclass(frozen=True)
class IssuedRecoveryCode:
    """Deliver the secret once to the operator; never log this field."""
    id: str
    secret: str = field(repr=False)


def _path(value):
    path = Path(value)
    if path.is_symlink():
        raise RecoveryProofError("recovery_proof_path_invalid")
    return path.resolve()


def _identity(operation_id, requested_by, reason):
    try:
        operation, actor = str(uuid.UUID(str(operation_id))), str(uuid.UUID(str(requested_by)))
    except (ValueError, TypeError, AttributeError):
        raise RecoveryProofError("recovery_proof_identity_invalid") from None
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise RecoveryProofError("recovery_proof_reason_invalid")
    return operation, actor, reason.strip()


def _json(value, secret=None):
    try:
        if not isinstance(value, dict):
            raise ValueError()
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 65536 or (secret and secret in encoded):
            raise ValueError()
        return encoded
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise RecoveryProofError("recovery_proof_payload_invalid") from None


def _verifier(secret, salt):
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 600000)


def issue_recovery_code(vault_path, database_path, backup_root, *, operation_id, requested_by, reason):
    """Provision a private vault and return an unpersisted 256-bit random code.

    Call only during controlled pre-incident provisioning. This function cannot
    recover a lost issuance response and never overwrites an existing vault.
    """
    operation, actor, reason = _identity(operation_id, requested_by, reason)
    vault, business, archives = _path(vault_path), _path(database_path), _path(backup_root)
    if vault == business or vault.is_relative_to(archives) or not business.is_file():
        raise RecoveryProofError("recovery_proof_location_invalid")
    code, salt = "tech1_" + secrets.token_urlsafe(32), secrets.token_bytes(16)
    verifier, identifier = _verifier(code, salt), str(uuid.uuid4())
    try:
        vault.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(vault, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        raise RecoveryProofError("recovery_proof_creation_refused") from None
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(vault)) as db, db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA user_version=1")
            db.execute("""CREATE TABLE recovery_events (
                operation_id TEXT PRIMARY KEY NOT NULL, request TEXT NOT NULL,
                receipt TEXT NOT NULL, recorded_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE recovery_proof (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL UNIQUE,
                database_path TEXT NOT NULL, backup_root TEXT NOT NULL,
                issued_operation TEXT NOT NULL UNIQUE, issued_by TEXT NOT NULL,
                reason TEXT NOT NULL, issued_at TEXT NOT NULL,
                salt BLOB NOT NULL CHECK(typeof(salt)='blob' AND length(salt)=16),
                verifier BLOB NOT NULL CHECK(typeof(verifier)='blob' AND length(verifier)=32),
                state TEXT NOT NULL CHECK(state IN ('available','consumed')),
                consumed_operation TEXT REFERENCES recovery_events(operation_id),
                CHECK((state='available' AND consumed_operation IS NULL) OR
                      (state='consumed' AND consumed_operation IS NOT NULL)))""")
            db.execute("INSERT INTO recovery_proof VALUES (1,?,?,?,?,?,?,?,?,?,'available',NULL)",
                       (identifier, str(business), str(archives), operation, actor, reason,
                        datetime.now(timezone.utc).isoformat(), salt, verifier))
        return IssuedRecoveryCode(identifier, code)
    except sqlite3.Error:
        # A partial file fails closed; never reset it on the next request.
        raise RecoveryProofError("recovery_proof_creation_failed") from None


@contextmanager
def _connection(vault):
    db = None
    try:
        db = sqlite3.connect(vault.as_uri() + "?mode=rw", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        if (db.execute("PRAGMA user_version").fetchone()[0] != 1
                or db.execute("PRAGMA journal_mode").fetchone()[0] != "delete"):
            raise RecoveryProofError("recovery_proof_format_invalid")
        yield db
    except sqlite3.Error:
        raise RecoveryProofError("recovery_proof_storage_unavailable") from None
    finally:
        if db is not None:
            db.set_authorizer(None)
            db.close()


def _effect_authorizer(action, name, column, database, source):
    if action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
        return sqlite3.SQLITE_OK if database == "restored" else sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class RecoveryTransaction:
    """A restricted business effect, or a historical receipt without write access."""
    def __init__(self, db, secret, receipt=None):
        self._db, self._secret, self._encoded_receipt = db, secret, None
        self.replayed = receipt is not None
        self.receipt = receipt

    def execute(self, sql, parameters=()):
        """Execute business DML only; protocol commits and vault writes are forbidden."""
        if self.replayed:
            raise RecoveryProofConflict("recovery_proof_replay_readonly")
        try:
            return self._db.execute(sql, parameters)
        except sqlite3.Error:
            raise RecoveryProofError("recovery_proof_effect_failed") from None

    def finish(self, receipt):
        """Stage a receipt; the effect and proof are committed only on clean exit."""
        if self.replayed or self._encoded_receipt is not None:
            raise RecoveryProofConflict("recovery_proof_receipt_conflict")
        self._encoded_receipt = _json(receipt, self._secret)
        self.receipt = json.loads(self._encoded_receipt)


@contextmanager
def redeem_recovery_code(vault_path, database_path, secret, *, operation_id, requested_by, reason, request):
    """Commit proof consumption and business effect together, or roll back both.

    SQLite multi-file atomicity requires real files and rollback journals. WAL
    is refused on both files. The caller must hold the external maintenance
    barrier for the entire context and must not perform separate side effects.
    """
    operation, actor, reason = _identity(operation_id, requested_by, reason)
    if not isinstance(secret, str) or not re.fullmatch(r"tech1_[A-Za-z0-9_-]{43}", secret):
        raise RecoveryProofError("recovery_proof_invalid")
    vault, business = _path(vault_path), _path(database_path)
    if vault == business or not business.is_file():
        raise RecoveryProofError("recovery_proof_database_invalid")
    encoded_request = _json({"requested_by": actor, "reason": reason,
                             "database_path": str(business), "request": request}, secret)
    with _connection(vault) as db:
        db.execute("ATTACH DATABASE ? AS restored", (business.as_uri() + "?mode=rw",))
        if db.execute("PRAGMA restored.journal_mode").fetchone()[0] != "delete":
            raise RecoveryProofError("recovery_proof_atomic_journal_required")
        db.execute("PRAGMA restored.synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM recovery_proof WHERE singleton=1").fetchone()
        if (row is None or row["database_path"] != str(business)
                or not isinstance(row["salt"], bytes) or len(row["salt"]) != 16
                or not isinstance(row["verifier"], bytes) or len(row["verifier"]) != 32
                or not hmac.compare_digest(_verifier(secret, row["salt"]), row["verifier"])):
            raise RecoveryProofError("recovery_proof_invalid")
        if operation == row["issued_operation"]:
            raise RecoveryProofConflict("recovery_proof_operation_conflict")
        previous = db.execute("SELECT request,receipt FROM recovery_events WHERE operation_id=?", (operation,)).fetchone()
        if previous:
            if previous["request"] != encoded_request or row["consumed_operation"] != operation:
                raise RecoveryProofConflict("recovery_proof_operation_conflict")
            proof = RecoveryTransaction(None, secret, json.loads(previous["receipt"]))
            yield proof
            return
        if row["state"] != "available":
            raise RecoveryProofUsed("recovery_proof_already_used")
        proof = RecoveryTransaction(db, secret)
        db.set_authorizer(_effect_authorizer)
        try:
            yield proof
        finally:
            db.set_authorizer(None)
        if proof._encoded_receipt is None:
            raise RecoveryProofError("recovery_proof_receipt_required")
        db.execute("INSERT INTO recovery_events VALUES (?,?,?,?)",
                   (operation, encoded_request, proof._encoded_receipt, datetime.now(timezone.utc).isoformat()))
        db.execute("UPDATE recovery_proof SET state='consumed', consumed_operation=? WHERE singleton=1", (operation,))
        db.commit()
