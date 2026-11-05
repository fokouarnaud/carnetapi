"""Current technical overview derived from archive and SMTP history, without writes."""
from django.db import transaction
from django.utils import timezone
from backups.services.delivery import jobs_with_delivery_status
from backups.services.jobs import authorize


@transaction.atomic
def backup_health(actor, *, at=None, session=None):
    authorize(actor, session)
    checked_at = timezone.now() if at is None else at
    if timezone.is_naive(checked_at):
        raise ValueError("aware_datetime_required")
    jobs = jobs_with_delivery_status()
    blockers = jobs.filter(abandoned_at__isnull=True, delivery_complete=False)
    blocked_count = blockers.count()
    blocking_job = blockers.order_by("created_at", "id").first() if blocked_count else None
    last_accepted_job = jobs.filter(delivery_complete=True).order_by("-delivery_completed_at", "-id").first()
    last_accepted_at = last_accepted_job.delivery_completed_at if last_accepted_job else None
    clock_inconsistent = last_accepted_at is not None and last_accepted_at > checked_at
    age = checked_at - last_accepted_at if last_accepted_at is not None and not clock_inconsistent else None
    return {"checked_at": checked_at, "blocked": blocked_count > 0, "blocked_count": blocked_count,
        "blocking_job": blocking_job, "last_accepted_job": last_accepted_job, "last_accepted_at": last_accepted_at,
        "last_accepted_age_seconds": age.days*86400+age.seconds if age is not None else None,
        "clock_inconsistent": clock_inconsistent}
