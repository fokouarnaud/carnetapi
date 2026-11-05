"""Idempotent removal of this archive's registered files after all SMTP acceptances."""
from django.db import transaction
from django.utils import timezone

from backups.models import BackupEvent, BackupJob
from backups.services.delivery import all_parts_accepted
from backups.services.jobs import BackupPreparationUnavailable, job_directory


def cleanup_if_complete(job_id):
    """Keep metadata/manifest; delete only exact registered part filenames.

    Filesystem work runs outside SQLite transactions. A crash or partial unlink
    failure can be resumed: missing registered files are already cleaned. Two
    callers may perform the same harmless unlinks; completion is recorded once.
    """
    with transaction.atomic():
        job = BackupJob.objects.get(id=job_id)
        if job.cleanup_state == "cleaned":
            return True
        if job.state != "ready" or job.abandoned_at is not None:
            return False
        parts = job.parts.order_by("number")
        if not all_parts_accepted(job):
            return False
        registered = list(parts)
        started = job.cleanup_state != "cleaning"
        job.cleanup_state, job.cleanup_error_code = "cleaning", ""
        job.cleanup_started_at = job.cleanup_started_at or timezone.now()
        job.save(update_fields=["cleanup_state", "cleanup_error_code", "cleanup_started_at", "updated_at"])
        if started:
            BackupEvent.objects.create(job=job, kind="cleanup_started", details={"part_count": job.part_count})
    try:
        directory = job_directory(job.id)
        paths = []
        for part in registered:
            expected = f"backup-{job.id}.part-{part.number:04d}-of-{job.part_count:04d}.axbkp"
            if part.filename != expected:
                raise BackupPreparationUnavailable()
            path = directory / expected
            if path.is_symlink() or path.resolve().parent != directory or (path.exists() and not path.is_file()):
                raise BackupPreparationUnavailable()
            paths.append(path)
        for path in paths:
            path.unlink(missing_ok=True)
    except (OSError, BackupPreparationUnavailable):
        with transaction.atomic():
            current = BackupJob.objects.get(id=job.id)
            if current.cleanup_state != "cleaned":
                current.cleanup_state, current.cleanup_error_code = "failed", "backup_cleanup_failed"
                current.save(update_fields=["cleanup_state", "cleanup_error_code", "updated_at"])
                BackupEvent.objects.create(job=current, kind="cleanup_failed", details={"code": "backup_cleanup_failed"})
        return False
    with transaction.atomic():
        current = BackupJob.objects.get(id=job.id)
        if current.cleanup_state != "cleaned":
            current.cleanup_state, current.cleanup_error_code, current.cleaned_at = "cleaned", "", timezone.now()
            current.save(update_fields=["cleanup_state", "cleanup_error_code", "cleaned_at", "updated_at"])
            BackupEvent.objects.create(job=current, kind="cleanup_completed", details={"part_count": current.part_count})
    return True
