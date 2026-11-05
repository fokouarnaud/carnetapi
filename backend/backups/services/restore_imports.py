"""Collect authenticated encrypted parts; neither verify the whole archive nor activate it."""
import hashlib
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import uuid

from django.conf import settings
from django.db import connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from backups.models import RestoreImport, RestoreImportPart
from backups.services.archive import BackupArchiveError, MAX_PART_BYTES, inspect_part
from backups.services.jobs import authorize
from common.services.errors import OperationConflict


class RestoreImportError(ValueError):
    """Stable diagnostics without key material, uploaded names or archive plaintext."""


def _root():
    base = Path(settings.BACKUP_ROOT).resolve()
    root = base / "restore-imports"
    if root.is_symlink():
        raise RestoreImportError("restore_import_storage_unavailable")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _directory(batch_id):
    root = _root()
    directory = root / str(uuid.UUID(str(batch_id)))
    if directory.is_symlink():
        raise RestoreImportError("restore_import_storage_unavailable")
    directory.mkdir(exist_ok=True, mode=0o700)
    return directory


def imported_part_path(batch, part):
    if part.batch_id != batch.id or not 1 <= part.number <= batch.part_count:
        raise RestoreImportError("restore_import_part_invalid")
    path = _directory(batch.id) / f"part-{part.number:05d}.axbkp"
    if path.is_symlink():
        raise RestoreImportError("restore_import_storage_unavailable")
    return path


def _matches(path, size, digest):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
        return False
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest() == digest


@sensitive_variables()
def collect_part(actor, batch_id, reason, upload, key):
    authorize(actor)
    if connection.in_atomic_block:
        raise RestoreImportError("restore_import_transaction_invalid")
    batch_id = uuid.UUID(str(batch_id))
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise RestoreImportError("restore_import_reason_invalid")
    reason = reason.strip()
    published = None
    try:
        root = _root()
        if shutil.disk_usage(root).free < 2*MAX_PART_BYTES:
            raise RestoreImportError("restore_import_storage_unavailable")
        with TemporaryDirectory(prefix="part-upload-", dir=root) as temporary:
            source = Path(temporary) / "encrypted-part"
            count = 0
            with source.open("xb") as target:
                os.chmod(source, 0o600)
                for block in upload.chunks(chunk_size=65536):
                    count += len(block)
                    if count > MAX_PART_BYTES:
                        raise RestoreImportError("encrypted_part_limit")
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            try:
                inspected = inspect_part(source, key)
            except BackupArchiveError as error:
                raise RestoreImportError(str(error)) from None
            header = {name: value for name, value in inspected.header.items() if name != "number"}
            known = set(MigrationLoader(None).disk_migrations)
            if (len(header["migrations"]) != len({tuple(row) for row in header["migrations"]})
                    or not {tuple(row) for row in header["migrations"]}.issubset(known)):
                raise RestoreImportError("backup_schema_incompatible")
            header_digest = hashlib.sha256(json.dumps(header, sort_keys=True,
                separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
            try:
                with transaction.atomic():
                    authorize(actor)
                    batch = RestoreImport.objects.filter(id=batch_id).first()
                    if batch is None:
                        batch = RestoreImport.objects.create(id=batch_id, requested_by=actor, reason=reason,
                            backup_id=header["backup_id"], part_count=header["total_parts"],
                            snapshot_bytes=header["snapshot_bytes"], header_sha256=header_digest, header=header)
                    elif (batch.requested_by_id != actor.id or batch.reason != reason
                            or batch.header_sha256 != header_digest or batch.header != header):
                        raise OperationConflict()
                    number = inspected.header["number"]
                    part = batch.parts.filter(number=number).first()
                    if part and (part.sha256 != inspected.sha256 or part.size_bytes != inspected.size_bytes):
                        raise OperationConflict()
                    destination = _directory(batch.id) / f"part-{number:05d}.axbkp"
                    try:
                        os.link(source, destination)
                        published = destination
                    except FileExistsError:
                        if not _matches(destination, inspected.size_bytes, inspected.sha256):
                            raise OperationConflict() from None
                    created = part is None
                    if created:
                        part = RestoreImportPart.objects.create(batch=batch, uploaded_by=actor, number=number,
                            size_bytes=inspected.size_bytes, sha256=inspected.sha256)
                    authorize(actor)
            except BaseException:
                if published is not None and published.exists() and published.samefile(source):
                    published.unlink()
                raise
        return batch, part, created
    except OSError:
        raise RestoreImportError("restore_import_storage_unavailable") from None


@transaction.atomic
def import_progress(actor, batch_id, *, page=1):
    authorize(actor)
    if type(page) is not int or not 1 <= page <= 656:
        raise RestoreImportError("restore_import_page_invalid")
    batch = get_object_or_404(RestoreImport, id=batch_id)
    received_count = batch.parts.filter(number__lte=batch.part_count).count()
    missing_count = batch.part_count-received_count
    page_count = max(1, (max(received_count, missing_count)+49)//50)
    if page > page_count:
        raise RestoreImportError("restore_import_page_invalid")
    offset = (page-1)*50
    received = list(batch.parts.filter(number__lte=batch.part_count).order_by("number").values_list("number", flat=True)[offset:offset+50])
    # Generate the complement in SQLite, then paginate before returning any rows.
    with connection.cursor() as cursor:
        cursor.execute("""WITH RECURSIVE expected(number) AS (
            SELECT 1 UNION ALL SELECT number+1 FROM expected WHERE number < %s
        ) SELECT expected.number FROM expected LEFT JOIN backups_restoreimportpart part
          ON part.batch_id=%s AND part.number=expected.number WHERE part.id IS NULL
          ORDER BY expected.number LIMIT 50 OFFSET %s""", [batch.part_count, batch.id.hex, offset])
        missing = [row[0] for row in cursor.fetchall()]
    authorize(actor)
    return {"batch": batch, "received": received, "missing": missing,
        "received_count": received_count, "missing_count": missing_count,
        "page": page, "page_count": page_count, "checked_at": timezone.now(),
        "collection_complete": received_count == batch.part_count,
        "archive_verified": False, "activation_allowed": False}
