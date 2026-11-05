"""One current snapshot or one SMTP step per authenticated external invocation."""
from datetime import datetime, time, timedelta, timezone as utc_timezone
import uuid
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from backups.models import BackupEvent, BackupJob, BackupScheduleRun, BackupSchedulerState
from backups.services.configuration import DOUALA, current_configuration
from backups.services.delivery import all_parts_accepted
from backups.services.dispatch import advance_dispatch, start_dispatch
from backups.services.email_parts import _address, BackupEmailError
from backups.services.jobs import authorize, configured_key, prepare_backup
from backups.services.scheduler_errors import SchedulerUnavailable
from identity.models import User
from backups.services.restore_gate import RestoreGateError
from backups.services.worker_gate import normal_database_work


def scheduler_actor():
    try:
        actor_id = uuid.UUID(str(getattr(settings, "BACKUP_SCHEDULER_ACTOR_ID", "")))
    except (ValueError, TypeError, AttributeError):
        raise SchedulerUnavailable() from None
    actor = User.objects.filter(id=actor_id, is_active=True, is_superuser=True).first()
    if actor is None:
        raise SchedulerUnavailable()
    return actor


def _continue_run(actor, run):
    job = run.job
    if job is None:
        job = BackupJob.objects.filter(operation_id=uuid.uuid5(run.id, "prepare")).first()
        if job is not None:
            run.job = job
            run.save(update_fields=["job", "updated_at"])
    if job is not None and job.abandoned_at is not None:
        return None
    if job is not None and job.state != "ready":
        # A crashed preparation cannot safely be assumed complete or regenerated.
        if job.state == "preparing":
            BackupJob.objects.filter(id=job.id, state="preparing").update(
                state="failed", failure_code="scheduler_preparation_interrupted", updated_at=timezone.now())
            BackupEvent.objects.create(job=job, kind="preparation_interrupted", details={"run": str(run.id)})
        return "blocked"
    if job is not None and all_parts_accepted(job):
        from backups.services.cleanup import cleanup_if_complete
        cleanup_if_complete(job.id)
        if run.dispatch_id:
            advance_dispatch(actor, job.id, run.dispatch_id)
        return None
    if run.recipient != settings.EMAIL_HOST_USER:
        return "blocked"
    if job is None:
        # Resume the same operation after a failure before snapshot metadata existed.
        if run.local_date != timezone.now().astimezone(DOUALA).date():
            run.failure_code = "scheduler_capture_not_started"
            run.save(update_fields=["failure_code", "updated_at"])
            return None
        return _prepare(actor, run)
    if run.dispatch_id is None:
        command, _ = start_dispatch(actor, job.id, uuid.uuid5(run.id, "dispatch"), _reason(run))
        run.dispatch = command
        run.save(update_fields=["dispatch", "updated_at"])
        return "prepared"
    command = advance_dispatch(actor, job.id, run.dispatch_id)
    if command.state == "accepted":
        return "accepted"
    return "incomplete" if command.state == "incomplete" else "progressed"


def _reason(run):
    return f"Daily server backup / Sauvegarde quotidienne : {run.local_date.isoformat()}"


def _prepare(actor, run):
    # Persisted actor preserves idempotency if the host operator changes mid-run.
    if actor.id != run.actor_id:
        return "blocked"
    job, _ = prepare_backup(actor, {"operation_id": uuid.uuid5(run.id, "prepare"),
        "recipient": run.recipient, "reason": _reason(run)})
    with transaction.atomic():
        run.job = job
        run.save(update_fields=["job", "updated_at"])
        BackupEvent.objects.get_or_create(job=job, kind="daily_capture", defaults={"details": {
            "run": str(run.id), "local_date": run.local_date.isoformat(),
            "scheduled_for": run.scheduled_for.isoformat(), "missed_day_count": run.missed_day_count,
            "missed_from": str(run.missed_from) if run.missed_from else None,
            "missed_to": str(run.missed_to) if run.missed_to else None}})
    command, _ = start_dispatch(actor, job.id, uuid.uuid5(run.id, "dispatch"), _reason(run))
    run.dispatch = command
    run.save(update_fields=["dispatch", "updated_at"])
    return "prepared"


def _work(actor):
    now = timezone.now()
    latest = BackupScheduleRun.objects.select_related("job", "dispatch").order_by("-sequence").first()
    if latest is not None:
        result = _continue_run(actor, latest)
        if result is not None:
            return result
    configuration = current_configuration()
    if not configuration.enabled:
        return "disabled"
    local_date = now.astimezone(DOUALA).date()
    scheduled_for = datetime.combine(local_date, time(configuration.local_hour, configuration.local_minute),
        tzinfo=DOUALA).astimezone(utc_timezone.utc)
    if latest is not None and latest.local_date > local_date:
        return "blocked"
    if now < scheduled_for or (latest is not None and latest.local_date == local_date
            and latest.job is not None and latest.job.abandoned_at is None):
        return "idle"
    for job in BackupJob.objects.filter(abandoned_at__isnull=True).exclude(cleanup_state="cleaned").iterator():
        if job.state != "ready" or not all_parts_accepted(job):
            return "blocked"
    configured_key()
    recipient = settings.EMAIL_HOST_USER
    try:
        _address(recipient)
    except BackupEmailError:
        raise SchedulerUnavailable() from None
    previous_capture_exists = latest is not None and latest.job is not None and latest.job.state == "ready"
    first = latest.local_date + timedelta(days=1 if previous_capture_exists else 0) if latest else None
    last = local_date - timedelta(days=1)
    count = (last - first).days + 1 if first is not None and first <= last else 0
    run = BackupScheduleRun.objects.create(sequence=latest.sequence + 1 if latest else 1, local_date=local_date,
        scheduled_for=scheduled_for, configuration_version=configuration.version, actor=actor, recipient=recipient,
        missed_day_count=count, missed_from=first if count else None, missed_to=last if count else None)
    return _prepare(actor, run)


def scheduler_tick():
    if connection.in_atomic_block:
        raise SchedulerUnavailable()
    try:
        with normal_database_work():
            return _scheduler_tick()
    except RestoreGateError:
        raise SchedulerUnavailable() from None


def _scheduler_tick():
    actor = scheduler_actor()
    lease = uuid.uuid4()
    with transaction.atomic():
        authorize(actor)
        state, _ = BackupSchedulerState.objects.get_or_create(singleton=1)
        now = timezone.now()
        if state.leased_until and state.leased_until > now:
            return "busy"
        state.lease_id, state.leased_until = lease, now + timedelta(minutes=15)
        state.save(update_fields=["lease_id", "leased_until", "updated_at"])
    try:
        return _work(actor)
    finally:
        BackupSchedulerState.objects.filter(id=state.id, lease_id=lease).update(
            lease_id=None, leased_until=None, updated_at=timezone.now())
