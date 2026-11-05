"""Durable daily runs and an expiring worker lease; no credentials are persisted."""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from common.model_base import UUIDRecord, _integer_columns


class BackupSchedulerState(UUIDRecord):
    singleton = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    lease_id = models.UUIDField(null=True, blank=True, editable=False)
    leased_until = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = _("Backup scheduler state")
        verbose_name_plural = _("Backup scheduler state")
        constraints = [
            models.CheckConstraint(condition=models.Q(singleton=1) & _integer_columns("singleton"),
                name="backup_scheduler_singleton"),
            models.CheckConstraint(condition=(models.Q(lease_id__isnull=True, leased_until__isnull=True) |
                models.Q(lease_id__isnull=False, leased_until__isnull=False)), name="backup_scheduler_lease_pair"),
        ]


class BackupScheduleRun(UUIDRecord):
    sequence = models.PositiveBigIntegerField(unique=True)
    local_date = models.DateField(_("Scheduled local date"))
    scheduled_for = models.DateTimeField(_("Scheduled for (UTC)"))
    configuration_version = models.PositiveBigIntegerField(default=0)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    recipient = models.EmailField(_("Recipient"))
    failure_code = models.CharField(_("Failure code"), max_length=64, blank=True)
    job = models.OneToOneField("backups.BackupJob", null=True, blank=True, on_delete=models.PROTECT)
    dispatch = models.OneToOneField("backups.BackupDispatch", null=True, blank=True, on_delete=models.PROTECT)
    missed_day_count = models.PositiveIntegerField(_("Missed days"), default=0)
    missed_from = models.DateField(_("First missed day"), null=True, blank=True)
    missed_to = models.DateField(_("Last missed day"), null=True, blank=True)

    class Meta:
        ordering = ["sequence"]
        verbose_name = _("Daily backup run")
        verbose_name_plural = _("Daily backup runs")
        constraints = [
            models.CheckConstraint(condition=models.Q(sequence__gt=0, configuration_version__gte=0,
                missed_day_count__gte=0) & _integer_columns("sequence", "configuration_version", "missed_day_count"),
                name="backup_schedule_integer_values"),
            models.CheckConstraint(condition=(
                models.Q(missed_day_count=0, missed_from__isnull=True, missed_to__isnull=True) |
                models.Q(missed_day_count__gt=0, missed_from__isnull=False, missed_to__isnull=False,
                    missed_to__gte=models.F("missed_from"), missed_to__lt=models.F("local_date"))),
                name="backup_schedule_gap_metadata"),
            models.CheckConstraint(condition=models.Q(dispatch__isnull=True) | models.Q(job__isnull=False),
                name="backup_schedule_dispatch_has_job"),
        ]
