"""Prepare a collected archive on a private copy; never replace the active DB."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid
from django.conf import settings
from django.db import connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from backups.models import ImportPreparation, ImportQuarantine, RestoreImport
from backups.services.jobs import authorize
from backups.services.restore_imports import imported_part_path, RestoreImportError
from backups.services.restore_preparation import prepare_restore
from backups.services.archive import BackupArchiveError, validate_snapshot
from backups.services.schema_audit import SchemaAuditError, schema_profile
from common.services.errors import OperationConflict


class ImportPreparationBusy(RestoreImportError):
    pass


def preparation_directory(record):
    base = Path(settings.BACKUP_ROOT).resolve()
    parent = base / "restore-prepared"
    directory = parent / str(record.id)
    if parent.is_symlink() or directory.is_symlink():
        raise RestoreImportError("restore_preparation_storage_unavailable")
    return directory


def _digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _checked_parts(batch):
    paths = []
    query = batch.parts.order_by("number")
    for offset in range(0, batch.part_count, 100):
        for part in query[offset:offset+100]:
            path = imported_part_path(batch, part)
            if not path.is_file() or path.stat().st_size != part.size_bytes or _digest(path) != part.sha256:
                raise RestoreImportError("restore_import_part_unavailable")
            paths.append(path)
    if len(paths) != batch.part_count:
        raise RestoreImportError("backup_parts_missing")
    return paths


def _checked_result(result, record, directory):
    path = directory / "candidate.sqlite3"
    manifest = result.manifest
    if (not isinstance(manifest, dict) or path.is_symlink() or result.path != path
            or not path.is_file() or manifest.get("backup_id") != str(record.batch.backup_id)
            or manifest.get("source_snapshot_sha256") != record.batch.header.get("snapshot_sha256")
            or any(manifest.get(name) is not True for name in ("schema_audit_checked", "migration_execution_checked"))
            or any(manifest.get(name) is not False for name in ("activation_allowed", "permissions_revalidated", "live_database_replaced"))):
        raise RestoreImportError("restore_preparation_receipt_invalid")
    size = manifest.get("candidate_bytes")
    digest = manifest.get("candidate_sha256")
    if (type(size) is not int or not 0 < size <= 500000000 or size != path.stat().st_size
            or not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest) or _digest(path) != digest):
        raise RestoreImportError("restore_preparation_receipt_invalid")
    return size, digest, manifest


def inspect_preparation(actor, preparation_id):
    """Check the surviving bytes against their recorded preparation, without writing."""
    from types import SimpleNamespace
    authorize(actor)
    if connection.in_atomic_block:
        raise RestoreImportError("restore_import_transaction_invalid")
    record = get_object_or_404(ImportPreparation.objects.select_related("batch"), id=preparation_id)
    if record.state != "prepared":
        raise RestoreImportError("restore_preparation_not_prepared")
    try:
        directory = preparation_directory(record)
        receipt = directory / "prepare_manifest.json"
        if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_size > 150000:
            raise RestoreImportError("restore_preparation_receipt_invalid")
        manifest = json.loads(receipt.read_text(encoding="utf-8"))
        if manifest != record.manifest:
            raise RestoreImportError("restore_preparation_receipt_invalid")
        path = directory / "candidate.sqlite3"
        size, digest, _manifest = _checked_result(SimpleNamespace(path=path, manifest=manifest), record, directory)
        if size != record.candidate_bytes or digest != record.candidate_sha256:
            raise RestoreImportError("restore_preparation_receipt_invalid")
        if validate_snapshot(path) != manifest.get("migrations_after"):
            raise RestoreImportError("restore_preparation_receipt_invalid")
        if schema_profile(path)["sha256"] != manifest.get("schema_sha256") or _digest(path) != digest:
            raise RestoreImportError("restore_preparation_receipt_invalid")
    except (OSError, ValueError, TypeError, RecursionError) as error:
        authorize(actor)
        if isinstance(error, RestoreImportError):
            raise
        if isinstance(error, (BackupArchiveError, SchemaAuditError)):
            raise RestoreImportError("restore_preparation_integrity_failed") from None
        raise RestoreImportError("restore_preparation_receipt_invalid") from None
    authorize(actor)
    return {"integrity_checked": True, "checked_at": timezone.now(),
        "candidate_bytes": size, "candidate_sha256": digest, "activation_allowed": False,
        "permissions_revalidated": False, "live_database_replaced": False}


_FAILURE_CODES = {
    "backup_authentication_failed", "invalid_backup_key", "backup_parts_missing", "backup_parts_mismatch",
    "backup_size_mismatch", "backup_digest_mismatch", "backup_schema_mismatch", "backup_schema_incompatible",
    "invalid_backup_compression", "encrypted_part_limit", "unsupported_backup_format", "invalid_backup_header",
    "invalid_sqlite_snapshot", "snapshot_integrity_failed", "snapshot_foreign_keys_failed",
    "restore_import_part_unavailable", "restore_import_part_invalid", "restore_preparation_receipt_invalid",
    "restore_output_exists", "restore_migration_timeout", "restore_worker_failed", "restore_migration_failed",
    "restore_migration_history_invalid", "restore_runtime_schema_mismatch", "restore_schema_incomplete",
    "restore_migration_graph_conflict", "restore_backward_migration_forbidden", "restore_runtime_unavailable",
    "restore_schema_mismatch", "restore_schema_reference_missing", "restore_schema_reference_stale",
    "restore_schema_reference_invalid", "restore_schema_unreadable", "restore_preparation_space_insufficient",
    "restore_preparation_storage_unavailable", "restore_candidate_size_limit", "backup_size_limit",
}


@sensitive_variables()
def prepare_import(actor, batch_id, operation_id, reason, key):
    authorize(actor)
    if connection.in_atomic_block:
        raise RestoreImportError("restore_import_transaction_invalid")
    operation_id = uuid.UUID(str(operation_id))
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise RestoreImportError("restore_import_reason_invalid")
    reason = reason.strip()
    with transaction.atomic():
        authorize(actor)
        batch = get_object_or_404(RestoreImport, id=batch_id)
        previous = ImportPreparation.objects.filter(operation_id=operation_id).first()
        if previous:
            if previous.batch_id != batch.id or previous.requested_by_id != actor.id or previous.reason != reason:
                raise OperationConflict()
            return previous, False
        if ImportPreparation.objects.filter(state="preparing").exists() or ImportQuarantine.objects.filter(state="quarantining").exists():
            raise ImportPreparationBusy("restore_preparation_in_progress")
        if batch.parts.count() != batch.part_count or batch.parts.filter(number__gt=batch.part_count).exists():
            raise RestoreImportError("backup_parts_missing")
        record = ImportPreparation.objects.create(batch=batch, operation_id=operation_id, requested_by=actor, reason=reason)
    try:
        paths = _checked_parts(batch)
        directory = preparation_directory(record)
        directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        required = 3*batch.snapshot_bytes+2*batch.header["compressed_bytes"]+15000000
        if shutil.disk_usage(directory.parent).free < required:
            raise RestoreImportError("restore_preparation_space_insufficient")
        expected = sorted([list(identity) for identity in MigrationLoader(None).disk_migrations])
        result = prepare_restore(paths, key, directory, expected_migrations=expected)
        size, digest, manifest = _checked_result(result, record, directory)
        with transaction.atomic():
            record.state = "prepared"
            record.finished_at = timezone.now()
            record.candidate_bytes = size
            record.candidate_sha256 = digest
            record.manifest = manifest
            record.save()
    except Exception as error:
        code = str(error) if str(error) in _FAILURE_CODES else "restore_import_preparation_failed"
        with transaction.atomic():
            ImportPreparation.objects.filter(id=record.id, state="preparing").update(
                state="failed", finished_at=timezone.now(), failure_code=code)
        record.refresh_from_db()
    # Preserve a completed surviving copy even when the requesting role was withdrawn.
    authorize(actor)
    return record, True
