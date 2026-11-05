"""Restoration access snapshots and append-only review decisions."""
from django.conf import settings
from django.db import models
from django.db.models.functions import Coalesce, Length, Trim
from django.db.models.lookups import GreaterThan, In
from django.utils.translation import gettext_lazy as _

from common.model_base import UUIDRecord, _integer_columns


PERMISSION_FIELDS = (
    "can_grant_credit", "can_collect_debt", "can_read_debt",
    "can_prepare_reminder", "can_read_cost", "can_read_cash_balance",
)


def _permission_booleans():
    condition = models.Q(archived_permissions__has_keys=list(PERMISSION_FIELDS))
    for key in PERMISSION_FIELDS:
        condition &= models.Q(Coalesce(In(models.Func(
            models.F("archived_permissions"), models.Value(f"$.{key}"),
            function="JSON_TYPE", output_field=models.CharField()), ("true", "false")), models.Value(False)))
    return condition


class RestoreAccessGeneration(UUIDRecord):
    """Identify a restoration without trusting identities from its archive."""
    operation_id = models.UUIDField(_("Operation UUID"), unique=True)
    backup_id = models.UUIDField(_("Backup UUID"))
    initiated_by = models.UUIDField(_("Author UUID"))
    current_slot = models.PositiveSmallIntegerField(default=1, null=True, unique=True)

    class Meta:
        verbose_name = _("Restoration access generation")
        verbose_name_plural = _("Restoration access generations")
        constraints = [models.CheckConstraint(condition=(models.Q(current_slot__isnull=True) |
            (models.Q(current_slot=1) & _integer_columns("current_slot"))), name="restore_access_current_slot")]


class RestoreAccessReview(UUIDRecord):
    """Preserve one archived account/shop scope pending current verification."""
    generation = models.ForeignKey(RestoreAccessGeneration, on_delete=models.PROTECT, related_name="reviews")
    shop = models.ForeignKey("identity.Shop", on_delete=models.PROTECT, related_name="restore_access_reviews")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="restore_access_reviews")
    archived_role = models.CharField(max_length=10)
    membership = models.ForeignKey("identity.Membership", on_delete=models.PROTECT, null=True, blank=True)
    was_active = models.BooleanField()
    archived_permissions = models.JSONField()

    class Meta:
        verbose_name = _("Restoration access review")
        verbose_name_plural = _("Restoration access reviews")
        constraints = [
            models.UniqueConstraint(fields=["generation", "shop", "user"], name="restore_access_review_scope"),
            models.CheckConstraint(condition=(models.Q(archived_role="owner", membership__isnull=True) |
                models.Q(archived_role__in=["manager", "seller"], membership__isnull=False)), name="restore_access_archived_role"),
            models.CheckConstraint(condition=_permission_booleans(), name="restore_access_permission_booleans"),
        ]


class RestoreAccessDecision(UUIDRecord):
    """Keep motivated decisions in version order without replacing history."""
    review = models.ForeignKey(RestoreAccessReview, on_delete=models.PROTECT, related_name="decisions")
    operation_id = models.UUIDField(_("Operation UUID"), unique=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    previous_version = models.PositiveBigIntegerField()
    version = models.PositiveBigIntegerField()
    state = models.CharField(max_length=10)
    reason = models.TextField(_("Reason"))
    evidence_ref = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ["version", "id"]
        verbose_name = _("Restoration access decision")
        verbose_name_plural = _("Restoration access decisions")
        constraints = [
            models.UniqueConstraint(fields=["review", "version"], name="restore_access_decision_version"),
            models.CheckConstraint(condition=models.Q(previous_version__gte=0, version=models.F("previous_version") + 1)
                & _integer_columns("previous_version", "version"), name="restore_access_decision_versions"),
            models.CheckConstraint(condition=models.Q(state__in=["confirmed", "refused"]), name="restore_access_decision_state"),
            models.CheckConstraint(condition=GreaterThan(Length(Trim("reason")), models.Value(0)), name="restore_access_decision_reason"),
        ]
