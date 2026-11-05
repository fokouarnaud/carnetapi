"""A recovered departure is reserved once until a recorded business decision."""
from django.db import models
from django.db.models.functions import Length,Trim
from django.db.models.lookups import GreaterThan
from django.utils.translation import gettext_lazy as _

from common.model_base import UUIDRecord,_integer_columns
from backups.recovery_models import _json_kind


class RecoverySaleHold(UUIDRecord):
    capture=models.OneToOneField("backups.RecoveryJournalOperation",on_delete=models.PROTECT,related_name="sale_hold")
    source_operation_id=models.UUIDField(unique=True)
    sale_id=models.UUIDField(unique=True)
    reservation_operation=models.OneToOneField("core.Activity",on_delete=models.PROTECT,related_name="recovery_sale_hold")
    net_total_fcfa=models.PositiveBigIntegerField()
    payments_fcfa=models.PositiveBigIntegerField()
    state=models.CharField(max_length=12,default="pending")
    version=models.PositiveBigIntegerField(default=0)

    class Meta:
        ordering=["created_at","id"]
        verbose_name=_("Recovered sale awaiting review")
        verbose_name_plural=_("Recovered sales awaiting review")
        indexes=[models.Index(fields=["state","created_at","id"],name="recovery_sale_hold_state_page")]
        constraints=[
            models.CheckConstraint(condition=models.Q(state__in=["pending","refused","validated"]),name="recovery_sale_hold_state"),
            models.CheckConstraint(condition=_integer_columns("version","net_total_fcfa","payments_fcfa"),name="recovery_sale_hold_integers"),
        ]


class RecoverySaleHoldDecision(UUIDRecord):
    hold=models.ForeignKey("backups.RecoverySaleHold",on_delete=models.PROTECT,related_name="decisions")
    capture=models.ForeignKey("backups.RecoveryJournalOperation",on_delete=models.PROTECT,related_name="sale_hold_decisions")
    operation_id=models.UUIDField(unique=True)
    reviewed_by=models.ForeignKey("core.User",on_delete=models.PROTECT,related_name="recovery_sale_hold_decisions")
    reviewed_installation=models.ForeignKey("identity.Installation",on_delete=models.PROTECT,related_name="recovery_sale_hold_decisions")
    previous_version=models.PositiveBigIntegerField()
    version=models.PositiveBigIntegerField()
    state=models.CharField(max_length=12)
    reason=models.TextField()
    request=models.JSONField()
    receipt=models.JSONField()
    integration_decision=models.OneToOneField("backups.RecoveryIntegrationDecision",on_delete=models.PROTECT,
        null=True,blank=True,related_name="sale_hold_decision")

    class Meta:
        ordering=["version","id"]
        verbose_name=_("Recovered sale review decision")
        verbose_name_plural=_("Recovered sale review decisions")
        constraints=[
            models.UniqueConstraint(fields=["hold","version"],name="recovery_sale_review_version_once"),
            models.CheckConstraint(condition=models.Q(previous_version__gte=0,version=models.F("previous_version")+1)
                &_integer_columns("previous_version","version"),name="recovery_sale_review_versions"),
            models.CheckConstraint(condition=models.Q(state__in=["pending","refused"],integration_decision__isnull=True)
                |models.Q(state="validated",integration_decision__isnull=False),name="recovery_sale_review_effect_link"),
            models.CheckConstraint(condition=GreaterThan(Length(Trim("reason")),models.Value(0)),name="recovery_sale_review_reason"),
            models.CheckConstraint(condition=_json_kind("request","object")&_json_kind("receipt","object"),name="recovery_sale_review_json"),
        ]
