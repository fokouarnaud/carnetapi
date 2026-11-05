"""Bounded, resumable whole-archive submission, with one durable request identity."""
from datetime import timedelta
import uuid
from django.db import connection, transaction
from django.db.models import Exists, OuterRef, Subquery, Q, F, Value, DateTimeField, ExpressionWrapper, Min
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from backups.models import BackupDispatch, BackupEvent, BackupJob, SendAttempt
from backups.services.abandonment import BackupAbandoned
from backups.services.cleanup import cleanup_if_complete
from backups.services.delivery import all_parts_accepted
from backups.services.jobs import authorize
from backups.services.sending import BackupSendConflict, BackupSendUnavailable, send_part
from common.services.errors import OperationConflict


def pending_parts(command):
    accepted = SendAttempt.objects.filter(part_id=OuterRef("pk"), state="accepted", recipient=command.job.recipient)
    latest = SendAttempt.objects.filter(part_id=OuterRef("pk")).order_by("-number")
    owned = SendAttempt.objects.filter(part_id=OuterRef("pk"), dispatch=command)
    return command.job.parts.filter(~Exists(accepted)).annotate(
        last_id=Subquery(latest.values("id")[:1]), last_state=Subquery(latest.values("state")[:1]),
        last_retry=Subquery(latest.values("retry_number")[:1]), has_current=Exists(owned),
        last_time=Coalesce(Subquery(latest.values("completed_at")[:1]), Subquery(latest.values("created_at")[:1]))
    ).annotate(due_at=ExpressionWrapper(F("last_time") + Value(timedelta(minutes=15)), output_field=DateTimeField()))


def eligible_parts(command, now):
    return pending_parts(command).filter(
        Q(last_id__isnull=True) | (Q(has_current=False) & ~Q(last_state="sending")) |
        (Q(last_retry__lt=3, due_at__lte=now, last_state__in=["failed", "uncertain", "sending"]) &
            (Q(has_current=True) | Q(last_state="sending"))))


def dispatch_progress(command):
    accepted = SendAttempt.objects.filter(part_id=OuterRef("pk"), state="accepted", recipient=command.job.recipient)
    count = command.job.parts.filter(Exists(accepted)).count()
    due = None
    if command.state == "active":
        if command.leased_until and command.leased_until > timezone.now():
            due = command.leased_until
        elif not eligible_parts(command, timezone.now()).exists():
            due = pending_parts(command).filter(Q(last_retry__lt=3) | Q(last_state="sending")).aggregate(value=Min("due_at"))["value"]
    return {"accepted_parts": count, "next_attempt_at": due}


def start_dispatch(actor, job_id, operation_id, reason, session=None):
    with transaction.atomic():
        authorize(actor, session)
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise ValidationError({"reason": "required"})
        reason = reason.strip()
        previous = BackupDispatch.objects.filter(operation_id=operation_id).select_related("job").first()
        if previous is not None:
            if previous.job_id != job_id or previous.requested_by_id != actor.id or previous.reason != reason:
                raise OperationConflict()
            return previous, False
        job = get_object_or_404(BackupJob, id=job_id)
        if job.abandoned_at:
            raise BackupAbandoned()
        if job.state != "ready":
            raise BackupSendUnavailable()
        if job.dispatches.filter(state="active").exists() or all_parts_accepted(job):
            raise BackupSendConflict()
        command = BackupDispatch.objects.create(job=job, operation_id=operation_id, requested_by=actor, reason=reason)
        BackupEvent.objects.create(job=job, kind="dispatch_requested", details={
            "dispatch": str(command.id), "operation_id": str(operation_id), "actor": str(actor.id), "reason": reason})
        return command, True


def _finish(command, state):
    command.state, command.finished_at = state, timezone.now()
    command.lease_id, command.leased_until = None, None
    command.save(update_fields=["state", "finished_at", "lease_id", "leased_until", "updated_at"])
    BackupEvent.objects.create(job=command.job, kind="dispatch_" + state, details={"dispatch": str(command.id)})


def _refresh_terminal(command):
    if command.state != "active":
        return
    if command.job.abandoned_at:
        _finish(command, "abandoned")
    elif all_parts_accepted(command.job):
        _finish(command, "accepted")
    elif not pending_parts(command).filter(Q(has_current=False) | Q(last_retry__lt=3) |
            Q(last_state="sending", due_at__gt=timezone.now())).exists():
        _finish(command, "incomplete")


def advance_dispatch(actor, job_id, dispatch_id, session=None):
    if connection.in_atomic_block:
        raise BackupSendUnavailable()
    lease = uuid.uuid4()
    with transaction.atomic():
        authorize(actor, session)
        command = get_object_or_404(BackupDispatch.objects.select_related("job"), id=dispatch_id, job_id=job_id)
        _refresh_terminal(command)
        if command.state == "active":
            now = timezone.now()
            if command.leased_until and command.leased_until > now:
                raise BackupSendConflict()
            command.lease_id, command.leased_until = lease, now + timedelta(minutes=15)
            command.save(update_fields=["lease_id", "leased_until", "updated_at"])
    if command.state != "active":
        if command.state == "accepted":
            cleanup_if_complete(command.job_id)
        authorize(actor, session)
        return command
    try:
        with transaction.atomic():
            command.refresh_from_db()
            command.job.refresh_from_db()
            if command.state != "active" or command.lease_id != lease:
                return command
            # An exhausted stale sender cannot be assumed rejected. Preserve
            # its unknown outcome before allowing an explicit new manual chain.
            expired = pending_parts(command).filter(last_state="sending", last_retry__gte=3, due_at__lte=timezone.now())
            for attempt_id in expired.order_by("number").values_list("last_id", flat=True)[:1]:
                if SendAttempt.objects.filter(id=attempt_id, state="sending").update(state="uncertain",
                        completed_at=timezone.now(), error_code="smtp_acceptance_unknown"):
                    BackupEvent.objects.create(job=command.job, kind="smtp_uncertain", details={"attempt": str(attempt_id)})
            selected = eligible_parts(command, timezone.now()).order_by("number").first()
            previous = None
            if selected is not None and selected.last_id and (selected.has_current or selected.last_state == "sending"):
                previous = selected.last_id
        if selected is not None:
            operation = uuid.uuid5(command.id, f"part:{selected.id}:previous:{previous or 'initial'}")
            send_part(actor, selected.id, operation, session=session, retry_of=previous, dispatch_id=command.id)
        with transaction.atomic():
            command.refresh_from_db()
            command.job.refresh_from_db()
            _refresh_terminal(command)
        if command.state == "accepted":
            cleanup_if_complete(command.job_id)
    finally:
        BackupDispatch.objects.filter(id=dispatch_id, lease_id=lease).update(lease_id=None, leased_until=None, updated_at=timezone.now())
    command.refresh_from_db()
    authorize(actor, session)
    return command
