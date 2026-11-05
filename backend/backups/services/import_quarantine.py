"""Persist authorized quarantine commands; keep file work outside SQL transactions."""
import hashlib
import json
from pathlib import Path
import re
import uuid
from django.conf import settings
from django.db import connection, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from backups.models import ImportPreparation, ImportQuarantine
from backups.services.jobs import authorize
from backups.services.import_preparation import inspect_preparation, preparation_directory
from backups.services.restore_imports import RestoreImportError
from backups.services.restore_quarantine import QUARANTINE_COUNT_KEYS, quarantine_restore
from common.services.errors import OperationConflict


class ImportQuarantineBusy(RestoreImportError):
    pass


def quarantine_directory(record):
    parent = Path(settings.BACKUP_ROOT).resolve()/"restore-quarantined"
    directory = parent/str(record.id)
    if parent.is_symlink() or directory.is_symlink():
        raise RestoreImportError("restore_quarantine_storage_unavailable")
    return directory


def _digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _checked_result(result, record, directory):
    manifest = result.manifest
    source = record.preparation
    candidate = directory/"candidate.sqlite3"
    receipt = directory/"quarantine_manifest.json"
    if (not isinstance(manifest, dict) or result.path != candidate or candidate.is_symlink() or not candidate.is_file()
            or receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_size > 150000
            or json.loads(receipt.read_text(encoding="utf-8")) != manifest
            or manifest.get("format") != "app-x-quarantined-restore" or type(manifest.get("format_version")) is not int
            or manifest["format_version"] != 1 or manifest.get("operation_id") != str(record.operation_id)
            or manifest.get("requested_by") != str(record.requested_by_id) or manifest.get("reason") != record.reason
            or manifest.get("backup_id") != str(source.batch.backup_id)
            or manifest.get("source_candidate_sha256") != source.candidate_sha256
            or manifest.get("source_receipt_sha256") != _digest(preparation_directory(source)/"prepare_manifest.json")
            or manifest.get("schema_sha256") != source.manifest.get("schema_sha256")
            or manifest.get("schema_reference_sha256") != source.manifest.get("schema_reference_sha256")
            or manifest.get("access_quarantined") is not True
            or any(manifest.get(name) is not False for name in ("activation_allowed", "permissions_revalidated", "live_database_replaced"))):
        raise RestoreImportError("restore_quarantine_receipt_invalid")
    for section in ("before", "after"):
        counts = manifest.get(section)
        if (not isinstance(counts, dict) or set(counts) != set(QUARANTINE_COUNT_KEYS)
                or any(type(value) is not int or value < 0 or (section == "after" and value != 0) for value in counts.values())):
            raise RestoreImportError("restore_quarantine_receipt_invalid")
    size, digest = manifest.get("candidate_bytes"), manifest.get("candidate_sha256")
    if (type(size) is not int or not 0 < size <= 500000000 or size != candidate.stat().st_size
            or not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest) or _digest(candidate) != digest):
        raise RestoreImportError("restore_quarantine_receipt_invalid")
    return size, digest, manifest


_FAILURE_CODES = {"restore_preparation_receipt_invalid", "restore_preparation_integrity_failed",
    "restore_preparation_not_prepared", "restore_preparation_storage_unavailable",
    "restore_quarantine_source_invalid", "restore_quarantine_request_invalid", "restore_quarantine_path_invalid",
    "restore_quarantine_storage_unavailable", "restore_output_exists", "restore_quarantine_space_insufficient",
    "restore_quarantine_incomplete", "restore_quarantine_failed", "restore_candidate_size_limit",
    "restore_quarantine_receipt_invalid"}


def quarantine_import(actor, preparation_id, operation_id, reason):
    authorize(actor)
    if connection.in_atomic_block:
        raise RestoreImportError("restore_import_transaction_invalid")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise RestoreImportError("restore_import_reason_invalid")
    reason, operation_id = reason.strip(), uuid.UUID(str(operation_id))
    with transaction.atomic():
        authorize(actor)
        source = get_object_or_404(ImportPreparation.objects.select_related("batch"), id=preparation_id)
        previous = ImportQuarantine.objects.filter(operation_id=operation_id).first()
        if previous:
            if previous.preparation_id != source.id or previous.requested_by_id != actor.id or previous.reason != reason:
                raise OperationConflict()
            return previous, False
        if ImportQuarantine.objects.filter(state="quarantining").exists() or ImportPreparation.objects.filter(state="preparing").exists():
            raise ImportQuarantineBusy("restore_quarantine_in_progress")
        if source.state != "prepared":
            raise RestoreImportError("restore_preparation_not_prepared")
        record = ImportQuarantine.objects.create(preparation=source, operation_id=operation_id, requested_by=actor, reason=reason)
    try:
        inspect_preparation(actor, source.id)
        directory = quarantine_directory(record)
        directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = quarantine_restore(preparation_directory(source), directory, operation_id=operation_id,
            requested_by=actor.id, reason=reason)
        size, digest, manifest = _checked_result(result, record, directory)
        with transaction.atomic():
            record.state, record.finished_at = "quarantined", timezone.now()
            record.candidate_bytes, record.candidate_sha256, record.manifest = size, digest, manifest
            record.save()
    except Exception as error:
        code = str(error) if str(error) in _FAILURE_CODES else "restore_import_quarantine_failed"
        with transaction.atomic():
            ImportQuarantine.objects.filter(id=record.id, state="quarantining").update(
                state="failed", finished_at=timezone.now(), failure_code=code)
        record.refresh_from_db()
    authorize(actor)
    return record, True
