"""Prepare one consistent encrypted server backup, without sending email."""
from contextlib import closing
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
import time
import uuid

from cryptography.fernet import Fernet
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, PermissionDenied

from backups.models import BackupEvent, BackupJob, BackupPart
from backups.services.archive import build_archive, verify_bundle
from common.services.errors import OperationConflict
from identity.models import User
from identity.services.authentication import require_live_session


logger = logging.getLogger(__name__)


class BackupPreparationUnavailable(APIException):
    status_code = 503
    default_code = "backup_preparation_unavailable"
    default_detail = {"code": "backup_preparation_unavailable"}


class BackupPreparationBusy(APIException):
    status_code = 409
    default_code = "backup_preparation_in_progress"
    default_detail = {"code": "backup_preparation_in_progress"}


def authorize(actor, session=None):
    if session is not None:
        require_live_session(actor, session.id, session.installation)
    if not User.objects.filter(id=actor.id, is_active=True, is_superuser=True).exists():
        raise PermissionDenied()


def job_directory(job_id):
    root = Path(settings.BACKUP_ROOT).resolve()
    path = (root / str(uuid.UUID(str(job_id)))).resolve()
    if not path.is_relative_to(root):
        raise BackupPreparationUnavailable()
    return path


def part_path(job, part):
    expected = f"backup-{job.id}.part-{part.number:04d}-of-{job.part_count:04d}.axbkp"
    if part.filename != expected:
        raise BackupPreparationUnavailable()
    root = job_directory(job.id)
    path = (root / expected).resolve()
    if not path.is_relative_to(root):
        raise BackupPreparationUnavailable()
    return path


def configured_key():
    key = getattr(settings, "BACKUP_ENCRYPTION_KEY", "")
    try:
        if not isinstance(key, (str, bytes)):
            raise ValueError()
        key = key.decode("ascii").strip() if isinstance(key, bytes) else key.strip()
        Fernet(key)
    except (ValueError, TypeError, UnicodeError):
        raise BackupPreparationUnavailable() from None
    other_secrets = [settings.SECRET_KEY, getattr(settings, "JWT_SIGNING_KEY", ""),
        settings.SIMPLE_JWT.get("SIGNING_KEY", ""), getattr(settings, "EMAIL_HOST_PASSWORD", "")]
    if key in [str(value).strip() for value in other_secrets]:
        raise BackupPreparationUnavailable()
    return key


def prepare_backup(actor, data, session=None):
    if connection.in_atomic_block:
        # SQLite's backup must run after committing the preparation metadata.
        raise BackupPreparationUnavailable()
    with transaction.atomic():
        authorize(actor, session)
        previous = BackupJob.objects.filter(operation_id=data["operation_id"]).first()
        if previous is not None:
            if previous.requested_by_id != actor.id or previous.recipient != data["recipient"] or previous.reason != data["reason"]:
                raise OperationConflict()
            if previous.state == "preparing":
                raise BackupPreparationBusy()
            if previous.state == "failed":
                raise BackupPreparationUnavailable()
            return previous, False
        key = configured_key()
        key_id = getattr(settings, "BACKUP_KEY_ID", "")
        if not isinstance(key_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key_id):
            raise BackupPreparationUnavailable()
        if BackupJob.objects.filter(state="preparing").exists():
            raise BackupPreparationBusy()
        job = BackupJob.objects.create(operation_id=data["operation_id"], requested_by=actor,
            installation_id=session.installation_id if session else None, recipient=data["recipient"],
            reason=data["reason"], key_id=key_id)
        BackupEvent.objects.create(job=job, kind="requested", details={"trigger": "manual"})
    built = None
    directory = None
    verified_ok = False
    try:
        directory = job_directory(job.id)
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        with TemporaryDirectory(prefix="work-", dir=directory) as temporary:
            snapshot = Path(temporary) / "snapshot.sqlite3"
            started = time.monotonic()

            def progress(status, remaining, total):
                if time.monotonic() - started > settings.BACKUP_SNAPSHOT_TIMEOUT_SECONDS:
                    raise TimeoutError()

            connection.ensure_connection()
            with closing(sqlite3.connect(snapshot)) as target:
                connection.connection.backup(target, pages=256, progress=progress, sleep=0.01)
            built = build_archive(snapshot, directory, key, backup_id=job.id, key_id=job.key_id,
                created_at=job.created_at, max_part_bytes=settings.BACKUP_PART_MAX_BYTES)
            verified = verify_bundle([directory / part["filename"] for part in built.parts], key,
                Path(temporary) / "check", expected_migrations=built.manifest["migrations"])
            if verified.manifest["snapshot_sha256"] != built.manifest["snapshot_sha256"]:
                raise ValueError("snapshot_digest_mismatch")
            verified_ok = True
        # Keep a durable, key-free manifest outside SQLite as well. A failure
        # registering metadata must not destroy a verified surviving archive.
        with (directory / "manifest.json").open("x", encoding="utf-8") as target:
            json.dump(built.manifest, target, sort_keys=True, separators=(",", ":"))
            target.flush()
            os.fsync(target.fileno())
        if os.name == "posix":
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        with transaction.atomic():
            BackupPart.objects.bulk_create([BackupPart(id=uuid.uuid5(job.id, f"part:{part['number']}"), job=job, **part)
                for part in built.parts])
            job.state = "ready"
            job.snapshot_bytes = built.manifest["snapshot_bytes"]
            job.snapshot_sha256 = built.manifest["snapshot_sha256"]
            job.part_count = built.manifest["total_parts"]
            job.archive_bytes = sum(part["size_bytes"] for part in built.parts)
            job.prepared_at = timezone.now()
            job.manifest = built.manifest
            job.save()
            BackupEvent.objects.create(job=job, kind="prepared", details={"part_count": job.part_count,
                "snapshot_sha256": job.snapshot_sha256, "archive_bytes": job.archive_bytes})
    except Exception as error:
        if built is not None and not verified_ok:
            for part in built.parts:
                path = (directory / part["filename"]).resolve()
                if path.is_relative_to(directory.resolve()):
                    path.unlink(missing_ok=True)
        with transaction.atomic():
            BackupJob.objects.filter(id=job.id).update(state="failed", failure_code="backup_preparation_failed")
            BackupEvent.objects.create(job=job, kind="failed", details={"code": "backup_preparation_failed"})
        logger.error("backup_preparation_failed", extra={"backup_id": str(job.id), "error_type": type(error).__name__})
        raise BackupPreparationUnavailable() from None
    authorize(actor, session)
    return job, True


def verify_part_file(job, part):
    path = part_path(job, part)
    if not path.is_file() or path.stat().st_size != part.size_bytes:
        raise BackupPreparationUnavailable()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(65536), b""):
            digest.update(block)
    if digest.hexdigest() != part.sha256:
        raise BackupPreparationUnavailable()
    return path
