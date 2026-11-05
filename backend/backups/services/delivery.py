"""Shared definition of complete, recorded SMTP acceptance."""
from django.db.models import BooleanField, Case, Count, F, Min, Max, Exists, OuterRef, Subquery, Value, When
from django.db.models.functions import Coalesce
from backups.models import BackupJob, BackupPart, SendAttempt


def all_parts_accepted(job):
    parts=job.parts.all()
    totals=parts.aggregate(count=Count("id"),first=Min("number"),last=Max("number"))
    accepted=SendAttempt.objects.filter(part_id=OuterRef("pk"),state="accepted",recipient=job.recipient)
    return (totals=={"count":job.part_count,"first":1,"last":job.part_count}
        and not parts.filter(~Exists(accepted)).exists())


def jobs_with_delivery_status(queryset=None):
    """Annotate complete SMTP acceptance and its first known completion, in SQL.

    Each registered part must be present and accepted for this archive's frozen
    recipient. Later duplicate acceptances cannot make an old archive look new.
    This describes SMTP acceptance, never email delivery or restorability.
    """
    jobs = BackupJob.objects.all() if queryset is None else queryset
    parts = BackupPart.objects.filter(job_id=OuterRef("pk")).order_by()
    totals = parts.values("job_id").annotate(total=Count("pk"), first=Min("number"), last=Max("number"))
    accepted = SendAttempt.objects.filter(part_id=OuterRef("pk"), state="accepted",
        recipient=OuterRef("job__recipient")).order_by("completed_at", "id")
    pending = parts.filter(~Exists(accepted))
    completion = parts.annotate(first_accepted_at=Subquery(accepted.values("completed_at")[:1])).values("job_id").annotate(
        completed_at=Max("first_accepted_at"))
    return jobs.annotate(
        registered_part_count=Coalesce(Subquery(totals.values("total")[:1]), Value(0)),
        first_part_number=Subquery(totals.values("first")[:1]),
        last_part_number=Subquery(totals.values("last")[:1]),
        has_unaccepted_parts=Exists(pending),
        delivery_completed_at=Subquery(completion.values("completed_at")[:1]),
    ).annotate(delivery_complete=Case(When(state="ready", part_count__gt=0,
        registered_part_count=F("part_count"), first_part_number=1, last_part_number=F("part_count"),
        has_unaccepted_parts=False, then=Value(True)), default=Value(False), output_field=BooleanField()))
