"""Historical origins and versioned, current-authority recovery decisions."""
from django.db import models
from django.db.models.functions import Length,Trim
from django.db.models.lookups import Exact,GreaterThan
from django.utils.translation import gettext_lazy as _

from common.model_base import UUIDRecord,_integer_columns
from backups.recovery_models import _json_kind


class RecoveredOperationOrigin(UUIDRecord):
    """A business writer is current; historical identities remain references."""
    capture=models.OneToOneField("backups.RecoveryJournalOperation",on_delete=models.PROTECT,related_name="business_origin")
    operation=models.OneToOneField("core.Activity",on_delete=models.PROTECT,related_name="recovered_origin")

    class Meta:
        verbose_name=_("Recovered operation origin")
        verbose_name_plural=_("Recovered operation origins")


class RecoveryIntegrationDecision(UUIDRecord):
    capture=models.ForeignKey("backups.RecoveryJournalOperation",on_delete=models.PROTECT,related_name="integration_decisions")
    business_operation=models.ForeignKey("core.Activity",on_delete=models.PROTECT,related_name="recovery_decisions")
    operation_id=models.UUIDField(unique=True)
    reviewed_by=models.ForeignKey("core.User",on_delete=models.PROTECT,related_name="recovery_integration_decisions")
    reviewed_installation=models.ForeignKey("identity.Installation",on_delete=models.PROTECT,related_name="recovery_integration_decisions")
    previous_version=models.PositiveBigIntegerField()
    version=models.PositiveBigIntegerField()
    outcome=models.CharField(max_length=20)
    reason=models.TextField()
    source_sha256=models.CharField(max_length=64)
    preview_sha256=models.CharField(max_length=64)
    request=models.JSONField()
    receipt=models.JSONField()

    class Meta:
        verbose_name=_("Recovery integration decision")
        verbose_name_plural=_("Recovery integration decisions")
        ordering=["version","id"]
        constraints=[
            models.UniqueConstraint(fields=["capture","version"],name="recovery_integration_version_once"),
            models.CheckConstraint(condition=models.Q(previous_version__gte=0,version=models.F("previous_version")+1)
                &_integer_columns("previous_version","version"),name="recovery_integration_versions"),
            models.CheckConstraint(condition=models.Q(outcome__in=["applied","already_known"]),name="recovery_integration_outcome"),
            models.CheckConstraint(condition=GreaterThan(Length(Trim("reason")),models.Value(0)),name="recovery_integration_reason"),
            models.CheckConstraint(condition=Exact(Length("source_sha256"),models.Value(64))
                &Exact(Length("preview_sha256"),models.Value(64)),name="recovery_integration_hash_lengths"),
            models.CheckConstraint(condition=_json_kind("request","object")&_json_kind("receipt","object"),name="recovery_integration_json_shapes"),
        ]
