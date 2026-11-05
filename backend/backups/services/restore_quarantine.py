"""Suspend archived credentials on a separate copy, without Django or activation."""
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import uuid
from backups.services.archive import MAX_SNAPSHOT_BYTES, validate_snapshot
from backups.services.restore_preparation import _write_json
from backups.services.schema_audit import schema_profile


class RestoreQuarantineError(ValueError):
    """A stable diagnostic without archived records or credentials."""


@dataclass(frozen=True)
class QuarantinedRestore:
    path: Path
    manifest: dict


def _digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _json(path, limit):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise RestoreQuarantineError("restore_quarantine_source_invalid")
    return json.loads(path.read_text(encoding="utf-8"))


def _source(directory):
    candidate = directory / "candidate.sqlite3"
    manifest_path = directory / "prepare_manifest.json"
    reference_path = Path(__file__).resolve().parents[2] / "restore_schema.json"
    try:
        if (directory.is_symlink() or candidate.is_symlink() or not candidate.is_file()
                or any(Path(str(candidate)+suffix).exists() for suffix in ("-wal", "-shm", "-journal"))):
            raise ValueError()
        manifest = _json(manifest_path, 150000)
        reference = _json(reference_path, 1000000)
        if (manifest.get("format") != "app-x-prepared-restore" or type(manifest.get("format_version")) is not int
                or manifest["format_version"] != 1
                or any(manifest.get(name) is not True for name in ("migration_execution_checked", "schema_audit_checked"))
                or any(manifest.get(name) is not False for name in ("activation_allowed", "permissions_revalidated", "live_database_replaced"))
                or type(manifest.get("candidate_bytes")) is not int
                or not 0 < manifest["candidate_bytes"] <= MAX_SNAPSHOT_BYTES
                or candidate.stat().st_size != manifest["candidate_bytes"]
                or not isinstance(manifest.get("candidate_sha256"), str)
                or not re.fullmatch("[0-9a-f]{64}", manifest["candidate_sha256"])
                or _digest(candidate) != manifest["candidate_sha256"]
                or _digest(reference_path) != manifest.get("schema_reference_sha256")
                or reference.get("format") != "app-x-sqlite-schema-reference"
                or reference.get("migration_code_sha256") != manifest.get("migration_code_sha256")
                or reference.get("schema", {}).get("sha256") != manifest.get("schema_sha256")
                or validate_snapshot(candidate) != manifest.get("migrations_after")
                or schema_profile(candidate) != reference.get("schema")):
            raise ValueError()
        uuid.UUID(manifest["backup_id"])
        return candidate, manifest, _digest(manifest_path)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise RestoreQuarantineError("restore_quarantine_source_invalid") from None


_COUNTS = {
    "active_users": "SELECT COUNT(*) FROM core_user WHERE typeof(is_active)!='integer' OR is_active!=0",
    "active_memberships": "SELECT COUNT(*) FROM core_membership WHERE typeof(is_active)!='integer' OR is_active!=0",
    "privileged_users": "SELECT COUNT(*) FROM core_user WHERE typeof(is_superuser)!='integer' OR is_superuser!=0 OR typeof(is_staff)!='integer' OR is_staff!=0",
    "group_assignments": "SELECT COUNT(*) FROM core_user_groups",
    "direct_permissions": "SELECT COUNT(*) FROM core_user_user_permissions",
    "unrevoked_installations": "SELECT COUNT(*) FROM core_installation WHERE revoked_at IS NULL",
    "unrevoked_sessions": "SELECT COUNT(*) FROM core_devicesession WHERE revoked_at IS NULL",
    "verified_shop_leases": "SELECT COUNT(*) FROM core_shopdeviceaccess WHERE revoked_at IS NULL OR verified_at IS NOT NULL OR offline_expires_at IS NOT NULL",
    "web_sessions": "SELECT COUNT(*) FROM django_session",
}
QUARANTINE_COUNT_KEYS = tuple(_COUNTS)


def _suspend(candidate, instant, *, generation_id, operation_id, backup_id, requested_by,snapshot_at=None):
    with closing(sqlite3.connect(candidate)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        with db:
            before = {name: db.execute(sql).fetchone()[0] for name, sql in _COUNTS.items()}
            # A new restore invalidates even approvals present in the archive.
            db.execute("UPDATE backups_restoreaccessgeneration SET current_slot=NULL")
            generation = uuid.UUID(str(generation_id)).hex
            db.execute("INSERT INTO backups_restoreaccessgeneration "
                "(id,created_at,updated_at,operation_id,backup_id,initiated_by,current_slot) VALUES (?,?,?,?,?,?,1)",
                [generation, instant, instant, uuid.UUID(str(operation_id)).hex,
                 uuid.UUID(str(backup_id)).hex, uuid.UUID(str(requested_by)).hex])
            names = ("can_grant_credit", "can_collect_debt", "can_read_debt",
                "can_prepare_reminder", "can_read_cost", "can_read_cash_balance")
            owners = db.execute("SELECT s.id,s.owner_id,u.is_active FROM core_shop s JOIN core_user u ON u.id=s.owner_id").fetchall()
            members = db.execute("SELECT m.shop_id,m.user_id,m.role,m.id,m.is_active," +
                ",".join("m."+name for name in names) +
                " FROM core_membership m JOIN core_shop s ON s.id=m.shop_id WHERE m.user_id!=s.owner_id").fetchall()
            insert = ("INSERT INTO backups_restoreaccessreview "
                "(id,created_at,updated_at,generation_id,shop_id,user_id,archived_role,membership_id,was_active,archived_permissions) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)")
            for shop, user, active in owners:
                db.execute(insert, [uuid.uuid4().hex, instant, instant, generation, shop, user,
                    "owner", None, active, json.dumps(dict.fromkeys(names, True))])
            for shop, user, role, membership, active, *permissions in members:
                db.execute(insert, [uuid.uuid4().hex, instant, instant, generation, shop, user,
                    role, membership, active, json.dumps(dict(zip(names, map(bool, permissions))))])
            from backups.services.recovery_snapshot import snapshot_recovery
            snapshot_recovery(db,generation_id,instant,snapshot_at=snapshot_at)
            db.execute("UPDATE core_user SET is_active=0, is_staff=0, is_superuser=0")
            db.execute("UPDATE core_membership SET is_active=0")
            db.execute("DELETE FROM core_user_groups")
            db.execute("DELETE FROM core_user_user_permissions")
            db.execute("UPDATE core_installation SET revoked_at=? WHERE revoked_at IS NULL", [instant])
            db.execute("UPDATE core_devicesession SET revoked_at=? WHERE revoked_at IS NULL", [instant])
            db.execute("UPDATE core_shopdeviceaccess SET revoked_at=COALESCE(revoked_at,?), verified_at=NULL, offline_expires_at=NULL", [instant])
            db.execute("DELETE FROM django_session")
            after = {name: db.execute(sql).fetchone()[0] for name, sql in _COUNTS.items()}
            if any(after.values()):
                raise RestoreQuarantineError("restore_quarantine_incomplete")
        return before, after


def quarantine_restore(prepared_dir, output_dir, *, operation_id, requested_by, reason):
    """Preserve the prepared source and publish one quarantined copy and its receipt.

    The caller is the hosting operator; UUIDs record attribution, not authorization.
    No archived account, including a technical account, remains enabled in the copy.
    """
    try:
        operation_id, requested_by = str(uuid.UUID(str(operation_id))), str(uuid.UUID(str(requested_by)))
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise RestoreQuarantineError("restore_quarantine_request_invalid") from None
    source_dir, output = Path(prepared_dir).absolute(), Path(output_dir).absolute()
    if output.resolve().is_relative_to(source_dir.resolve()) or source_dir.resolve().is_relative_to(output.resolve()):
        raise RestoreQuarantineError("restore_quarantine_path_invalid")
    source, original, receipt_digest = _source(source_dir)
    try:
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        raise RestoreQuarantineError("restore_output_exists") from None
    published = []
    try:
        if shutil.disk_usage(output).free < 2*original["candidate_bytes"]+15000000:
            raise RestoreQuarantineError("restore_quarantine_space_insufficient")
        with TemporaryDirectory(prefix="quarantine-work-", dir=output) as directory:
            work = Path(directory).resolve()
            if not work.is_relative_to(output.resolve()):
                raise RestoreQuarantineError("restore_quarantine_path_invalid")
            candidate = work / "candidate.sqlite3"
            shutil.copyfile(source, candidate)
            os.chmod(candidate, 0o600)
            if _digest(candidate) != original["candidate_sha256"]:
                raise RestoreQuarantineError("restore_quarantine_source_invalid")
            instant = datetime.now(timezone.utc)
            generation_id = uuid.uuid4()
            before, after = _suspend(candidate, instant.replace(tzinfo=None).isoformat(sep=" "),
                generation_id=generation_id, operation_id=operation_id, backup_id=original["backup_id"], requested_by=requested_by,
                snapshot_at=datetime.fromisoformat(original["backup_created_at"]) if original.get("backup_created_at") else None)
            if (validate_snapshot(candidate) != original["migrations_after"]
                    or schema_profile(candidate)["sha256"] != original["schema_sha256"]
                    or _digest(source) != original["candidate_sha256"]
                    or _digest(source_dir / "prepare_manifest.json") != receipt_digest):
                raise RestoreQuarantineError("restore_quarantine_source_invalid")
            size = candidate.stat().st_size
            if not 0 < size <= MAX_SNAPSHOT_BYTES:
                raise RestoreQuarantineError("restore_candidate_size_limit")
            manifest = {"format": "app-x-quarantined-restore", "format_version": 1,
                "operation_id": operation_id, "requested_by": requested_by, "reason": reason.strip(),
                "quarantined_at": instant.isoformat(), "backup_id": original["backup_id"],
                "restore_generation_id": str(generation_id),
                "source_candidate_sha256": original["candidate_sha256"], "source_receipt_sha256": receipt_digest,
                "candidate_bytes": size, "candidate_sha256": _digest(candidate),
                "schema_sha256": original["schema_sha256"], "schema_reference_sha256": original["schema_reference_sha256"],
                "before": before, "after": after, "access_quarantined": True,
                "activation_allowed": False, "permissions_revalidated": False, "live_database_replaced": False}
            with candidate.open("r+b") as target:
                target.flush()
                os.fsync(target.fileno())
            final = output / "candidate.sqlite3"
            os.link(candidate, final)
            published.append(final)
            _write_json(output / "quarantine_manifest.json", manifest, owned=published)
        return QuarantinedRestore(final, manifest)
    except BaseException as error:
        for path in reversed(published):
            path.unlink(missing_ok=True)
        try:
            output.rmdir()
        except OSError:
            pass
        if isinstance(error, sqlite3.Error):
            raise RestoreQuarantineError("restore_quarantine_failed") from None
        raise
