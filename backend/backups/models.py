"""Global backup metadata; encryption keys and plaintext are never stored here."""
import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from common.model_base import UUIDRecord, _integer_columns


class BackupJob(UUIDRecord):
    created_at = models.DateTimeField(_("Created at (UTC)"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at (UTC)"), auto_now=True)
    operation_id = models.UUIDField(_("Operation UUID"), unique=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_("Requested by"), on_delete=models.PROTECT)
    installation_id = models.UUIDField(_("Installation UUID"), null=True, blank=True)
    reason = models.TextField(_("Reason"))
    recipient = models.EmailField(_("Recipient"))
    state = models.CharField(_("Preparation state"), max_length=12, default="preparing", choices=[
        ("preparing", _("Preparing")), ("ready", _("Prepared")), ("failed", _("Failed"))])
    preparation_slot = models.PositiveSmallIntegerField(default=1, editable=False)
    key_id = models.CharField(_("Key identifier"), max_length=64)
    snapshot_sha256 = models.CharField(_("Snapshot SHA-256"), max_length=64, blank=True)
    snapshot_bytes = models.PositiveBigIntegerField(_("Snapshot bytes"), default=0)
    archive_bytes = models.PositiveBigIntegerField(_("Archive bytes"), default=0)
    part_count = models.PositiveIntegerField(_("Part count"), default=0)
    prepared_at = models.DateTimeField(_("Prepared at (UTC)"), null=True, blank=True)
    failure_code = models.CharField(_("Failure code"), max_length=64, blank=True)
    manifest = models.JSONField(_("Manifest"), default=dict)
    cleanup_state = models.CharField(_("Archive cleanup state"), max_length=12, default="waiting", choices=[
        ("waiting", _("Waiting for all SMTP acceptances")), ("cleaning", _("Cleaning archive files")),
        ("failed", _("Archive cleanup failed")), ("cleaned", _("Archive files cleaned"))])
    cleanup_started_at = models.DateTimeField(_("Cleanup started at (UTC)"), null=True, blank=True)
    cleaned_at = models.DateTimeField(_("Cleaned at (UTC)"), null=True, blank=True)
    cleanup_error_code = models.CharField(_("Cleanup error code"), max_length=64, blank=True)
    abandoned_at = models.DateTimeField(_("Abandoned at (UTC)"), null=True, blank=True)
    abandoned_by = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_("Abandoned by"),
        related_name="abandoned_backups", null=True, blank=True, on_delete=models.PROTECT)
    abandon_operation_id = models.UUIDField(_("Abandonment operation UUID"), null=True, blank=True, unique=True)
    abandon_reason = models.TextField(_("Abandonment reason"), blank=True)



    class Meta:
        verbose_name = _("Server backup")
        verbose_name_plural = _("Server backups")
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=(
                models.Q(abandoned_at__isnull=True, abandoned_by__isnull=True, abandon_operation_id__isnull=True, abandon_reason="") |
                (models.Q(abandoned_at__isnull=False, abandoned_by__isnull=False, abandon_operation_id__isnull=False,
                    state__in=["ready", "failed"], cleanup_state="waiting") & ~models.Q(abandon_reason=""))
            ), name="backup_abandonment_metadata"),
            models.CheckConstraint(condition=models.Q(state__in=["preparing", "ready", "failed"]), name="backup_job_known_state"),
            models.CheckConstraint(condition=(
                models.Q(cleanup_state="waiting", cleanup_started_at__isnull=True, cleaned_at__isnull=True, cleanup_error_code="") |
                (models.Q(state="ready", cleanup_started_at__isnull=False) & (
                    models.Q(cleanup_state="cleaning", cleaned_at__isnull=True, cleanup_error_code="") |
                    models.Q(cleanup_state="cleaned", cleaned_at__isnull=False, cleanup_error_code="") |
                    (models.Q(cleanup_state="failed", cleaned_at__isnull=True) & ~models.Q(cleanup_error_code=""))
                ))), name="backup_cleanup_state_metadata"),
            models.CheckConstraint(condition=models.Q(preparation_slot=1) & _integer_columns("preparation_slot", "snapshot_bytes", "archive_bytes", "part_count"), name="backup_job_integer_metadata"),
            models.UniqueConstraint(fields=["preparation_slot"], condition=models.Q(state="preparing"), name="backup_one_snapshot_preparing"),
            models.CheckConstraint(condition=(models.Q(state="ready", part_count__gt=0, snapshot_bytes__gt=0,
                archive_bytes__gt=0, prepared_at__isnull=False, failure_code="") |
                models.Q(state="preparing", part_count=0, prepared_at__isnull=True, failure_code="") |
                (models.Q(state="failed", part_count=0, prepared_at__isnull=True) & ~models.Q(failure_code=""))),
                name="backup_job_state_metadata"),
        ]

    def __str__(self):
        return str(self.id)


class BackupPart(UUIDRecord):
    job = models.ForeignKey(BackupJob, on_delete=models.PROTECT, related_name="parts")
    number = models.PositiveIntegerField()
    filename = models.CharField(max_length=120)
    size_bytes = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)

    class Meta:
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(fields=["job", "number"], name="backup_part_number_once"),
            models.UniqueConstraint(fields=["job", "filename"], name="backup_part_filename_once"),
            models.CheckConstraint(condition=models.Q(number__gt=0, size_bytes__gt=0, size_bytes__lte=15000000)
                & _integer_columns("number", "size_bytes"), name="backup_part_bounded_integer"),
        ]


class BackupEvent(UUIDRecord):
    job = models.ForeignKey(BackupJob, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=32)
    details = models.JSONField(default=dict)

    class Meta:
        ordering = ["created_at", "id"]


class SendAttempt(UUIDRecord):
    """One SMTP submission; acceptance is separate from actual delivery."""
    dispatch = models.ForeignKey("BackupDispatch", verbose_name=_("Archive dispatch"), null=True, blank=True,
        on_delete=models.PROTECT, related_name="attempts")
    created_at = models.DateTimeField(_("Created at (UTC)"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at (UTC)"), auto_now=True)
    part = models.ForeignKey(BackupPart, verbose_name=_("Backup part"), on_delete=models.PROTECT, related_name="send_attempts")
    operation_id = models.UUIDField(_("Operation UUID"), unique=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_("Requested by"), on_delete=models.PROTECT)
    previous = models.ForeignKey("self", verbose_name=_("Previous attempt"), null=True, blank=True, on_delete=models.PROTECT, related_name="retries")
    number = models.PositiveIntegerField(_("Attempt number"))
    retry_number = models.PositiveSmallIntegerField(_("Automatic retry number"), default=0)
    state = models.CharField(_("SMTP state"), max_length=12, default="sending", choices=[
        ("sending", _("Sending")), ("accepted", _("Accepted by SMTP, delivery unconfirmed")),
        ("failed", _("Failed")), ("uncertain", _("SMTP acceptance uncertain"))])
    sender = models.EmailField(_("Sender"))
    recipient = models.EmailField(_("Recipient"))
    encoded_bytes = models.PositiveIntegerField(_("Encoded message bytes"))
    wire_sha256 = models.CharField(_("Message SHA-256"), max_length=64)
    possible_duplicate = models.BooleanField(_("Possible duplicate"), default=False)
    completed_at = models.DateTimeField(_("Completed at (UTC)"), null=True, blank=True)
    error_code = models.CharField(_("Error code"), max_length=64, blank=True)

    class Meta:
        verbose_name = _("SMTP send attempt")
        verbose_name_plural = _("SMTP send attempts")
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["part", "number"], name="backup_send_number_once"),
            models.UniqueConstraint(fields=["part"], condition=models.Q(state="sending"), name="backup_send_one_inflight"),
            models.UniqueConstraint(fields=["previous"], condition=models.Q(previous__isnull=False), name="backup_send_one_retry_child"),
            models.CheckConstraint(condition=models.Q(number__gt=0, encoded_bytes__gt=0, retry_number__gte=0, retry_number__lte=3)
                & _integer_columns("number", "retry_number", "encoded_bytes"), name="backup_send_integer_bounds"),
            models.CheckConstraint(condition=(models.Q(retry_number=0, previous__isnull=True) |
                models.Q(retry_number__gte=1, previous__isnull=False)), name="backup_send_retry_parent"),
            models.CheckConstraint(condition=(models.Q(state="sending", completed_at__isnull=True, error_code="") |
                models.Q(state="accepted", completed_at__isnull=False, error_code="") |
                (models.Q(state__in=["failed", "uncertain"], completed_at__isnull=False) & ~models.Q(error_code=""))),
                name="backup_send_outcome_metadata"),
        ]


class BackupDispatch(UUIDRecord):
    """One explicit whole-archive request, resumable in bounded SMTP steps."""
    created_at = models.DateTimeField(_("Created at (UTC)"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at (UTC)"), auto_now=True)
    job = models.ForeignKey(BackupJob, verbose_name=_("Server backup"), on_delete=models.PROTECT, related_name="dispatches")
    operation_id = models.UUIDField(_("Operation UUID"), unique=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_("Requested by"), on_delete=models.PROTECT)
    reason = models.TextField(_("Dispatch reason"))
    state = models.CharField(_("Dispatch state"), max_length=12, default="active", choices=[
        ("active", _("Dispatch active")), ("accepted", _("Accepted by SMTP, delivery unconfirmed")),
        ("incomplete", _("Dispatch incomplete")), ("abandoned", _("Dispatch abandoned"))])
    finished_at = models.DateTimeField(_("Finished at (UTC)"), null=True, blank=True)
    lease_id = models.UUIDField(null=True, blank=True, editable=False)
    leased_until = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = _("Archive dispatch")
        verbose_name_plural = _("Archive dispatches")
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["job"], condition=models.Q(state="active"), name="backup_dispatch_one_active"),
            models.CheckConstraint(condition=(models.Q(state="active", finished_at__isnull=True) |
                models.Q(state__in=["accepted", "incomplete", "abandoned"], finished_at__isnull=False)), name="backup_dispatch_state_metadata"),
            models.CheckConstraint(condition=(models.Q(lease_id__isnull=True, leased_until__isnull=True) |
                models.Q(state="active", lease_id__isnull=False, leased_until__isnull=False)), name="backup_dispatch_lease_metadata"),
        ]

    def __str__(self):
        return str(self.id)


BACKUP_CONFIGURATION_ID = uuid.UUID("f7935e2a-5096-4974-97b2-b8dd09bc2210")


class BackupConfiguration(UUIDRecord):
    """One versioned daily schedule; SMTP credentials remain outside SQLite."""
    id = models.UUIDField(primary_key=True, default=BACKUP_CONFIGURATION_ID, editable=False)
    singleton = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    enabled = models.BooleanField(_("Daily backups enabled"), default=True)
    local_hour = models.PositiveSmallIntegerField(_("Local hour"), default=2)
    local_minute = models.PositiveSmallIntegerField(_("Local minute"), default=0)
    timezone_name = models.CharField(_("Schedule time zone"), max_length=64, default="Africa/Douala", editable=False)
    version = models.PositiveBigIntegerField(_("Configuration version"), default=1)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_("Updated by"), null=True,
        blank=True, on_delete=models.PROTECT, related_name="backup_configuration_updates")
    created_at = models.DateTimeField(_("Created at (UTC)"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at (UTC)"), auto_now=True)

    class Meta:
        verbose_name = _("Daily backup configuration")
        verbose_name_plural = _("Daily backup configuration")
        constraints = [
            models.CheckConstraint(condition=models.Q(singleton=1, id=BACKUP_CONFIGURATION_ID,
                timezone_name="Africa/Douala", local_hour__gte=0, local_hour__lte=23,
                local_minute__gte=0, local_minute__lte=59, version__gte=1, enabled__in=[True, False])
                & _integer_columns("singleton", "local_hour", "local_minute", "version", "enabled"), name="backup_configuration_exact_values"),
        ]

    def __str__(self):
        return str(self.id)


class BackupConfigurationChange(UUIDRecord):
    configuration = models.ForeignKey(BackupConfiguration, verbose_name=_("Daily backup configuration"),
        on_delete=models.PROTECT, related_name="changes")
    operation_id = models.UUIDField(_("Operation UUID"), unique=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name=_("Author"), on_delete=models.PROTECT)
    reason = models.TextField(_("Reason"))
    previous_version = models.PositiveBigIntegerField(_("Previous version"))
    version = models.PositiveBigIntegerField(_("Configuration version"))
    payload = models.JSONField()
    before = models.JSONField(_("Before"))
    after = models.JSONField(_("After"))
    created_at = models.DateTimeField(_("Created at (UTC)"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at (UTC)"), auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name = _("Backup configuration change")
        verbose_name_plural = _("Backup configuration changes")
        constraints = [
            models.UniqueConstraint(fields=["configuration", "version"], name="backup_configuration_change_version_once"),
            models.CheckConstraint(condition=models.Q(previous_version__gte=0, version=models.F("previous_version")+1)
                & _integer_columns("previous_version", "version"), name="backup_configuration_change_versions"),
        ]


from backups.scheduler_models import BackupScheduleRun, BackupSchedulerState  # noqa: E402,F401
from backups.restore_models import ImportPreparation, ImportQuarantine, RestoreImport, RestoreImportPart  # noqa: E402,F401
from backups.access_models import RestoreAccessGeneration, RestoreAccessReview, RestoreAccessDecision  # noqa: E402,F401
from backups.recovery_models import RestoreSession, RecoveryDevice, RecoveryJournalDeposit, RecoveryJournalOperation  # noqa: E402,F401
from backups.integration_models import RecoveredOperationOrigin, RecoveryIntegrationDecision  # noqa: E402,F401
from backups.sale_hold_models import RecoverySaleHold, RecoverySaleHoldDecision  # noqa: E402,F401
from backups.collection_review_models import RecoveryCollectionReview, RecoveryCollectionReviewDecision  # noqa: E402,F401
from backups.hosting_models import HostingConfiguration, HostingConfigurationChange, HostingReminder, HostingReminderAttempt, HostingObservation, HostingReminderRepair  # noqa: E402,F401
from backups.closure_models import RecoveryClosure  # noqa: E402,F401
