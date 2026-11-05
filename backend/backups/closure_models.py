"""A motivated technical closure keeps missing data separate from business facts."""
from django.db import models
from django.db.models.functions import Length, Trim
from django.db.models.lookups import GreaterThan
from django.utils.translation import gettext_lazy as _

from backups.recovery_models import _json_kind
from common.model_base import UUIDRecord


class RecoveryClosure(UUIDRecord):
    """Freeze a partial recovery declaration without asserting exhaustive recovery."""
    session=models.OneToOneField("backups.RestoreSession",on_delete=models.PROTECT,related_name="closure")
    operation_id=models.UUIDField(unique=True)
    closed_by=models.ForeignKey("core.User",on_delete=models.PROTECT,related_name="recovery_closures")
    closed_installation=models.ForeignKey("identity.Installation",on_delete=models.PROTECT,
        null=True,blank=True,related_name="recovery_closures")
    kind=models.CharField(max_length=12)
    reason=models.TextField()
    request=models.JSONField()
    snapshot=models.JSONField()
    missing_devices=models.JSONField()
    receipt=models.JSONField()

    class Meta:
        verbose_name=_("Partial recovery closure")
        verbose_name_plural=_("Partial recovery closures")
        ordering=["created_at","id"]
        constraints=[
            models.CheckConstraint(condition=models.Q(kind="partial"),name="recovery_closure_partial_only"),
            models.CheckConstraint(condition=GreaterThan(Length(Trim("reason")),models.Value(0)),name="recovery_closure_reason"),
            models.CheckConstraint(condition=_json_kind("request","object")&_json_kind("snapshot","object")
                &_json_kind("receipt","object")&_json_kind("missing_devices","array")
                &GreaterThan(models.Func(models.F("missing_devices"),function="JSON_ARRAY_LENGTH",output_field=models.IntegerField()),models.Value(0)),
                name="recovery_closure_json_shapes"),
        ]
