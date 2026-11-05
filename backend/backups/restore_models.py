"""Encrypted import inventory, separate from restore preparation and activation."""
from django.conf import settings
from django.db import models
from django.db.models.lookups import Exact
from django.db.models.functions import Coalesce
from django.utils.translation import gettext_lazy as _
from common.model_base import UUIDRecord, _integer_columns


def _prepared_receipt():
    """Use deterministic SQLite JSON expressions with exact JSON scalar types."""
    types = (("migration_execution_checked", "true"), ("schema_audit_checked", "true"),
        ("activation_allowed", "false"), ("permissions_revalidated", "false"),
        ("live_database_replaced", "false"), ("candidate_bytes", "integer"), ("candidate_sha256", "text"))
    condition = models.Q(manifest__has_keys=[name for name, _kind in types])
    for name, kind in types:
        condition &= Exact(models.Func(models.F("manifest"), models.Value("$."+name),
            function="JSON_TYPE", output_field=models.CharField()), models.Value(kind))
    for name, field in (("candidate_bytes", models.BigIntegerField()), ("candidate_sha256", models.CharField())):
        condition &= Exact(models.Func(models.F("manifest"), models.Value("$."+name),
            function="JSON_EXTRACT", output_field=field), models.F(name))
    return condition


class RestoreImport(UUIDRecord):
    created_at = models.DateTimeField(_("Created at (UTC)"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at (UTC)"), auto_now=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        verbose_name=_("Requested by"))
    reason = models.TextField(_("Reason"))
    backup_id = models.UUIDField(_("Backup UUID"))
    part_count = models.PositiveIntegerField(_("Part count"))
    snapshot_bytes = models.PositiveBigIntegerField(_("Snapshot bytes"))
    header_sha256 = models.CharField(_("Archive metadata SHA-256"), max_length=64)
    header = models.JSONField(_("Authenticated archive metadata"))

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = _("Server restore import")
        verbose_name_plural = _("Server restore imports")
        constraints = [
            models.CheckConstraint(condition=models.Q(part_count__gte=1, part_count__lte=32768,
                snapshot_bytes__gt=0, snapshot_bytes__lte=500000000)
                & _integer_columns("part_count", "snapshot_bytes"), name="restore_import_bounds"),
            models.CheckConstraint(condition=~models.Q(reason=""), name="restore_import_reason_required"),
        ]

    def __str__(self):
        return str(self.id)


class RestoreImportPart(UUIDRecord):
    created_at = models.DateTimeField(_("Created at (UTC)"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at (UTC)"), auto_now=True)
    batch = models.ForeignKey(RestoreImport, related_name="parts", on_delete=models.PROTECT, verbose_name=_("Import UUID"))
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        verbose_name=_("Uploaded by"))
    number = models.PositiveIntegerField(_("Part number"))
    size_bytes = models.PositiveIntegerField(_("Encrypted bytes"))
    sha256 = models.CharField(_("Encrypted part SHA-256"), max_length=64)

    class Meta:
        ordering = ["number"]
        verbose_name = _("Imported encrypted part")
        verbose_name_plural = _("Imported encrypted parts")
        constraints = [
            models.UniqueConstraint(fields=["batch", "number"], name="restore_import_part_once"),
            models.CheckConstraint(condition=models.Q(number__gte=1, number__lte=32768,
                size_bytes__gt=0, size_bytes__lte=15000000) & _integer_columns("number", "size_bytes"),
                name="restore_import_part_bounds"),
        ]

    def __str__(self):
        return f"{self.batch_id} / {self.number}"


class ImportPreparation(UUIDRecord):
    created_at = models.DateTimeField(_("Created at (UTC)"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at (UTC)"), auto_now=True)
    batch = models.ForeignKey(RestoreImport, related_name="preparations", on_delete=models.PROTECT, verbose_name=_("Import UUID"))
    operation_id = models.UUIDField(_("Operation UUID"), unique=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name=_("Requested by"))
    reason = models.TextField(_("Reason"))
    state = models.CharField(_("Preparation state"), max_length=12, default="preparing",
        choices=[("preparing", _("Preparing")), ("prepared", _("Copy prepared")), ("failed", _("Failed"))])
    worker_slot = models.PositiveSmallIntegerField(default=1, editable=False)
    finished_at = models.DateTimeField(_("Finished at (UTC)"), null=True, blank=True)
    failure_code = models.CharField(_("Failure code"), max_length=64, blank=True)
    candidate_bytes = models.PositiveBigIntegerField(_("Prepared copy bytes"), default=0)
    candidate_sha256 = models.CharField(_("Prepared copy SHA-256"), max_length=64, blank=True)
    manifest = models.JSONField(_("Preparation receipt"), default=dict)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = _("Imported archive preparation")
        verbose_name_plural = _("Imported archive preparations")
        constraints = [
            models.UniqueConstraint(fields=["worker_slot"], condition=models.Q(state="preparing"), name="restore_prepare_one_worker"),
            models.CheckConstraint(condition=models.Q(worker_slot=1) & _integer_columns("worker_slot", "candidate_bytes"), name="restore_prepare_integer_values"),
            models.CheckConstraint(condition=~models.Q(reason=""), name="restore_prepare_reason_required"),
            models.CheckConstraint(condition=(
                models.Q(state="preparing", finished_at__isnull=True, failure_code="", candidate_bytes=0, candidate_sha256="", manifest={}) |
                (models.Q(state="failed", finished_at__isnull=False, candidate_bytes=0, candidate_sha256="", manifest={}) & ~models.Q(failure_code="")) |
                (models.Q(state="prepared", finished_at__isnull=False, failure_code="", candidate_bytes__gt=0,
                    candidate_bytes__lte=500000000) & _prepared_receipt())
            ), name="restore_prepare_state_evidence"),
        ]

    def __str__(self):
        return str(self.id)


def _quarantined_receipt():
    types = (("access_quarantined", "true"), ("activation_allowed", "false"),
        ("permissions_revalidated", "false"), ("live_database_replaced", "false"),
        ("candidate_bytes", "integer"), ("candidate_sha256", "text"))
    counts = ("active_users", "active_memberships", "privileged_users", "group_assignments",
        "direct_permissions", "unrevoked_installations", "unrevoked_sessions", "verified_shop_leases", "web_sessions")
    condition = models.Q()
    for name, kind in (*types, *(("after."+name, "integer") for name in counts)):
        value_type = models.Func(models.F("manifest"), models.Value("$."+name), function="JSON_TYPE", output_field=models.CharField())
        condition &= Exact(Coalesce(value_type, models.Value("")), models.Value(kind))
    for name, field in (("candidate_bytes", models.BigIntegerField()), ("candidate_sha256", models.CharField())):
        condition &= Exact(models.Func(models.F("manifest"), models.Value("$."+name), function="JSON_EXTRACT", output_field=field), models.F(name))
    for name in counts:
        condition &= Exact(models.Func(models.F("manifest"), models.Value("$.after."+name),
            function="JSON_EXTRACT", output_field=models.IntegerField()), models.Value(0))
    return condition


class ImportQuarantine(UUIDRecord):
    created_at = models.DateTimeField(_("Created at (UTC)"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at (UTC)"), auto_now=True)
    preparation = models.ForeignKey(ImportPreparation, related_name="quarantines", on_delete=models.PROTECT, verbose_name=_("Preparation UUID"))
    operation_id = models.UUIDField(_("Operation UUID"), unique=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name=_("Requested by"))
    reason = models.TextField(_("Reason"))
    state = models.CharField(_("Quarantine state"), max_length=12, default="quarantining", choices=[
        ("quarantining", _("Quarantining")), ("quarantined", _("Copy quarantined")), ("failed", _("Failed"))])
    worker_slot = models.PositiveSmallIntegerField(default=1, editable=False)
    finished_at = models.DateTimeField(_("Finished at (UTC)"), null=True, blank=True)
    failure_code = models.CharField(_("Failure code"), max_length=64, blank=True)
    candidate_bytes = models.PositiveBigIntegerField(_("Quarantined copy bytes"), default=0)
    candidate_sha256 = models.CharField(_("Quarantined copy SHA-256"), max_length=64, blank=True)
    manifest = models.JSONField(_("Quarantine receipt"), default=dict)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = _("Imported archive quarantine")
        verbose_name_plural = _("Imported archive quarantines")
        constraints = [
            models.UniqueConstraint(fields=["worker_slot"], condition=models.Q(state="quarantining"), name="restore_quarantine_one_worker"),
            models.CheckConstraint(condition=models.Q(worker_slot=1) & _integer_columns("worker_slot", "candidate_bytes"), name="restore_quarantine_integers"),
            models.CheckConstraint(condition=~models.Q(reason=""), name="restore_quarantine_reason"),
            models.CheckConstraint(condition=(
                models.Q(state="quarantining", finished_at__isnull=True, failure_code="", candidate_bytes=0, candidate_sha256="", manifest={}) |
                (models.Q(state="failed", finished_at__isnull=False, candidate_bytes=0, candidate_sha256="", manifest={}) & ~models.Q(failure_code="")) |
                (models.Q(state="quarantined", finished_at__isnull=False, failure_code="", candidate_bytes__gt=0,
                    candidate_bytes__lte=500000000) & _quarantined_receipt())
            ), name="restore_quarantine_evidence"),
        ]

    def __str__(self):
        return str(self.id)
