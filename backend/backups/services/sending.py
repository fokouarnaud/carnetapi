"""Durable part submissions; no SQLite transaction spans an SMTP call."""
from datetime import timedelta
import hashlib
import smtplib

from django.conf import settings
from django.core.mail.backends.smtp import EmailBackend
from django.db import transaction, connection
from django.db.models import Max
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException

from backups.models import BackupDispatch, BackupEvent, BackupJob, BackupPart, SendAttempt
from backups.services.email_parts import build_part_email
from backups.services.jobs import authorize
from backups.services.abandonment import BackupAbandoned
from backups.services.cleanup import cleanup_if_complete
from common.services.errors import OperationConflict


class BackupSendConflict(APIException):
    status_code = 409
    default_code = "backup_send_not_due_or_busy"
    default_detail = {"code": "backup_send_not_due_or_busy"}


class BackupSendUnavailable(APIException):
    status_code = 503
    default_code = "backup_send_unavailable"
    default_detail = {"code": "backup_send_unavailable"}


def submit_smtp(message):
    """Submit the already measured wire bytes; a successful return means SMTP acceptance."""
    backend = EmailBackend(fail_silently=False)
    try:
        backend.open()
        refused = backend.connection.sendmail(message.sender, [message.recipient], message.wire_bytes)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        # QUIT is cleanup. Its failure does not undo a recorded DATA acceptance
        # or replace the original submission error with a misleading QUIT error.
        try:
            backend.close()
        except (smtplib.SMTPException, OSError):
            pass


def _check(actor, part_id, operation_id, retry_of, session, dispatch_id=None):
    authorize(actor, session)
    existing = SendAttempt.objects.filter(operation_id=operation_id).first()
    if existing is not None:
        if existing.part_id != part_id or existing.requested_by_id != actor.id or existing.previous_id != retry_of or existing.dispatch_id != dispatch_id:
            raise OperationConflict()
        return existing, None, None
    part = get_object_or_404(BackupPart.objects.select_related("job"), id=part_id)
    if dispatch_id is not None and not BackupDispatch.objects.filter(id=dispatch_id, job_id=part.job_id, state="active").exists():
        raise BackupSendConflict()
    if part.job.abandoned_at is not None:
        raise BackupAbandoned()
    if part.job.state != "ready":
        raise BackupSendUnavailable()
    accepted = part.send_attempts.filter(state="accepted").first()
    if accepted is not None:
        return accepted, part, None
    latest = part.send_attempts.order_by("-number").first()
    previous = None
    if retry_of is not None:
        previous = get_object_or_404(SendAttempt, id=retry_of, part=part)
        child = previous.retries.first()
        if child is not None:
            return child, part, None
        due_from = previous.completed_at or previous.created_at
        if (latest.id != previous.id or previous.retry_number >= 3
                or previous.state not in {"failed", "uncertain", "sending"}
                or timezone.now() < due_from + timedelta(minutes=15)):
            raise BackupSendConflict()
    elif latest is not None and latest.state == "sending":
        raise BackupSendConflict()
    return None, part, previous


def _existing_result(attempt):
    if attempt.state == "accepted":
        cleanup_if_complete(attempt.part.job_id)
    return attempt, False


def send_part(actor, part_id, operation_id, *, session=None, retry_of=None, transport=None, dispatch_id=None):
    """Send once or retry a known failed/uncertain attempt after its delay.

    Explicit manual requests start a new retry budget; retry_of follows the
    existing chain and cannot exceed three automatic retries. A stale sending
    attempt is uncertain, never silently assumed accepted or failed.
    """
    if connection.in_atomic_block:
        raise BackupSendUnavailable()
    with transaction.atomic():
        existing, part, previous = _check(actor, part_id, operation_id, retry_of, session, dispatch_id)
    if existing is not None:
        return _existing_result(existing)
    possible_duplicate = (previous is not None and previous.state in {"sending", "uncertain"}) or (
        previous is None and part.send_attempts.filter(state__in=["sending", "uncertain"]).exists())
    message = build_part_email(part.job, part, sender=settings.EMAIL_HOST_USER,
        max_message_bytes=getattr(settings, "BACKUP_EMAIL_MAX_BYTES", 25000000),
        possible_duplicate=possible_duplicate)
    with transaction.atomic():
        existing, part, previous = _check(actor, part_id, operation_id, retry_of, session, dispatch_id)
        if existing is None:
            if previous is not None and previous.state == "sending":
                previous.state, previous.error_code = "uncertain", "smtp_acceptance_unknown"
                previous.completed_at = timezone.now()
                previous.save(update_fields=["state", "error_code", "completed_at", "updated_at"])
                BackupEvent.objects.create(job=part.job, kind="smtp_uncertain", details={"attempt": str(previous.id)})
            attempt = SendAttempt.objects.create(part=part, operation_id=operation_id, requested_by=actor, dispatch_id=dispatch_id,
                previous=previous, number=(part.send_attempts.aggregate(value=Max("number"))["value"] or 0)+1,
                retry_number=previous.retry_number+1 if previous else 0,
                sender=message.sender, recipient=message.recipient, encoded_bytes=len(message.wire_bytes),
                possible_duplicate=possible_duplicate,
                wire_sha256=hashlib.sha256(message.wire_bytes).hexdigest())
            BackupEvent.objects.create(job=part.job, kind="smtp_started", details={
                "attempt": str(attempt.id), "part": part.number, "retry_number": attempt.retry_number, "possible_duplicate": attempt.possible_duplicate})
    if existing is not None:
        return _existing_result(existing)
    authorize(actor, session)
    abandoned_error = None
    try:
        if BackupJob.objects.filter(id=part.job_id, abandoned_at__isnull=False).exists():
            raise BackupAbandoned()
        (transport or submit_smtp)(message)
        state, error_code = "accepted", ""
    except BackupAbandoned as error:
        state, error_code, abandoned_error = "failed", "backup_abandoned", error
    except (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused):
        state, error_code = "failed", "smtp_rejected"
    except (smtplib.SMTPException, OSError):
        state, error_code = "uncertain", "smtp_acceptance_unknown"
    with transaction.atomic():
        attempt.state, attempt.error_code, attempt.completed_at = state, error_code, timezone.now()
        if state == "accepted" and part.send_attempts.filter(state="accepted").exclude(id=attempt.id).exists():
            attempt.possible_duplicate = True
        attempt.save(update_fields=["state", "error_code", "completed_at", "possible_duplicate", "updated_at"])
        BackupEvent.objects.create(job=part.job, kind="smtp_"+state,
            details={"attempt": str(attempt.id), "part": part.number, "code": error_code, "possible_duplicate": attempt.possible_duplicate})
    if state == "accepted":
        cleanup_if_complete(part.job_id)
    if abandoned_error is not None:
        raise abandoned_error
    authorize(actor, session)
    return attempt, True
