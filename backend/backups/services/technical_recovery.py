"""Controlled technical bootstrap; no archive activation or business revalidation."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import uuid

from django.contrib.auth.hashers import make_password, check_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db.migrations.loader import MigrationLoader

from identity.models import User
from .archive import validate_snapshot
from .recovery_proof import RecoveryProofError, issue_recovery_code, redeem_recovery_code, _identity
from .restore_gate import hold_control, RestoreGateError
from .restore_migrations import schema_metadata
from .restore_quarantine import _COUNTS
from .schema_audit import audit_schema
from .access_coverage import checked_generation


class TechnicalRecoveryError(RecoveryProofError):
    """A technical recovery prerequisite is missing; no rights are restored."""


@contextmanager
def _control(gate_path, database_path, mode, version):
    try:
        with hold_control(gate_path, database_path, required_mode=mode, expected_version=version) as state:
            yield state
    except RestoreGateError:
        raise TechnicalRecoveryError("technical_recovery_gate_unavailable") from None
    except sqlite3.Error:
        raise TechnicalRecoveryError("technical_recovery_database_unavailable") from None


def provision_technical_recovery(gate_path, vault_path, database_path, backup_root, *,
                                operation_id, requested_by, reason, gate_version):
    """Prepare a code only while the gate is open and the actor is currently technical.

    Hosting-console authentication remains a prerequisite for the caller. The
    returned secret must be delivered once, outside logs, then held off-server.
    """
    operation, actor, reason = _identity(operation_id, requested_by, reason)
    with _control(gate_path, database_path, "open", gate_version):
        with closing(sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            current = db.execute("SELECT 1 FROM core_user WHERE id=? AND is_active=1 AND is_staff=1 AND is_superuser=1",
                                 (uuid.UUID(actor).hex,)).fetchone()
            if current is None:
                raise TechnicalRecoveryError("technical_recovery_actor_forbidden")
        return issue_recovery_code(vault_path, database_path, backup_root,
                                   operation_id=operation, requested_by=actor, reason=reason)


def _credentials(username, password, language):
    if (not isinstance(username, str) or not isinstance(password, str)
            or not 1 <= len(password) <= 4096 or language not in ("fr", "en")):
        raise TechnicalRecoveryError("technical_recovery_credentials_invalid")
    username = User.normalize_username(username)
    user = User(username=username)
    try:
        User._meta.get_field("username").run_validators(username)
        if not username:
            raise ValidationError("empty")
        validate_password(password, user)
    except ValidationError:
        raise TechnicalRecoveryError("technical_recovery_credentials_invalid") from None
    return username


def _schema(database_path):
    try:
        migrations = validate_snapshot(database_path)
        loader = MigrationLoader(None)
        if sorted(tuple(row) for row in migrations) != sorted(loader.disk_migrations):
            raise ValueError()
        metadata = schema_metadata(SimpleNamespace(loader=loader), migrations)
        return audit_schema(database_path, Path(__file__).resolve().parents[2] / "restore_schema.json", metadata)
    except (OSError, ValueError, TypeError, KeyError):
        raise TechnicalRecoveryError("technical_recovery_schema_invalid") from None


def _replay_account(database_path, receipt, username, password, language):
    try:
        identifier = uuid.UUID(receipt["user_id"])
        with closing(sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("""SELECT username,password,is_active,is_staff,is_superuser,language
                FROM core_user WHERE id=?""", (identifier.hex,)).fetchone()
        valid = (row is not None and (row[0], *row[2:]) == (username, 1, 1, 1, language)
                 and check_password(password, row[1]))
    except (ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        raise TechnicalRecoveryError("technical_recovery_receipt_unavailable")
    return receipt


def _access_counts(proof):
    # Qualify every trusted count, including COUNT(*) without a WHERE clause:
    # SQLite otherwise omits its schema in the authorizer callback.
    return {name: proof.execute(sql.replace("FROM ", "FROM restored.", 1)).fetchone()[0]
            for name, sql in _COUNTS.items()}


def bootstrap_technical_recovery(gate_path, vault_path, database_path, secret, *, username, password, language,
                                 operation_id, requested_by, reason, gate_version):
    """Create one fresh technical account with proof consumption in the same commit.

    Archived accounts remain inactive. The maintenance gate remains closed and
    business rights are not revalidated. This service never replaces a database.
    """
    operation, actor, reason = _identity(operation_id, requested_by, reason)
    username = _credentials(username, password, language)
    if not isinstance(secret, str) or not 1 <= len(secret) <= 200:
        raise TechnicalRecoveryError("recovery_proof_invalid")
    # Bind retries to the password without persisting plaintext or a dictionary-
    # attackable unsalted password hash. The random proof is not stored in clear.
    try:
        password_proof = hmac.new(secret.encode("utf-8"), password.encode("utf-8"), hashlib.sha256).hexdigest()
    except UnicodeError:
        raise TechnicalRecoveryError("technical_recovery_credentials_invalid") from None
    request = {"username": username, "language": language, "password_proof": password_proof,
               "gate_version": gate_version, "action": "create_fresh_technical_account"}
    with _control(gate_path, database_path, "maintenance", gate_version):
        audit = _schema(database_path)
        with redeem_recovery_code(vault_path, database_path, secret, operation_id=operation,
                requested_by=actor, reason=reason, request=request) as proof:
            if proof.replayed:
                return _replay_account(database_path, proof.receipt, username, password, language)
            if any(_access_counts(proof).values()):
                raise TechnicalRecoveryError("technical_recovery_access_not_quarantined")
            try:
                generation_id=checked_generation(proof,prefix="restored.",require_pending=True)
            except (ValueError,TypeError):
                raise TechnicalRecoveryError("technical_recovery_access_not_quarantined") from None
            if proof.execute("SELECT 1 FROM restored.core_user WHERE username=?", (username,)).fetchone():
                raise TechnicalRecoveryError("technical_recovery_username_unavailable")
            identifier, instant = uuid.uuid4(), datetime.now(timezone.utc)
            proof.execute("""INSERT INTO restored.core_user
                (id,password,last_login,is_superuser,username,first_name,last_name,email,
                 is_staff,is_active,date_joined,phone,language,language_version)
                 VALUES (?,?,NULL,1,?,'','','',1,1,?,NULL,?,1)""",
                (identifier.hex, make_password(password), username,
                 instant.replace(tzinfo=None).isoformat(sep=" "), language))
            after = _access_counts(proof)
            if after != {name: int(name in ("active_users", "privileged_users")) for name in _COUNTS}:
                raise TechnicalRecoveryError("technical_recovery_access_invalid")
            receipt = {"operation_id": operation, "requested_by": actor, "reason": reason,
                       "user_id": str(identifier), "username": username, "language": language,
                       "created_at": instant.isoformat(), "technical_account_created": True,
                       "permissions_revalidated": False, "activation_allowed": False,
                       "business_operations_allowed": False, "gate_version": gate_version,
                       "schema_sha256": audit["schema_sha256"]}
            receipt["restore_generation_id"]=generation_id
            proof.finish(receipt)
        return receipt
