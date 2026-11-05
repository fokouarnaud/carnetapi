"""Recovery observations are separate from restored identity and permissions."""
from django.db import models
from django.db.models.functions import Length,Trim
from django.db.models.lookups import Exact,GreaterThan
from django.utils.translation import gettext_lazy as _

from common.model_base import UUIDRecord,_integer_columns


class RestoreSession(UUIDRecord):
    generation=models.OneToOneField("backups.RestoreAccessGeneration",on_delete=models.PROTECT,related_name="recovery_session")
    snapshot_at=models.DateTimeField(null=True,blank=True)
    inventory_recorded_at=models.DateTimeField(null=True,blank=True)

    class Meta:
        verbose_name=_("Restoration recovery session")
        verbose_name_plural=_("Restoration recovery sessions")


class RecoveryDevice(UUIDRecord):
    session=models.ForeignKey(RestoreSession,on_delete=models.PROTECT,related_name="devices")
    shop=models.ForeignKey("identity.Shop",on_delete=models.PROTECT,related_name="recovery_devices")
    # UUID observations can survive absence of an identity in the restored DB.
    # They never create an account, installation, lease, or access grant.
    origin_installation_id=models.UUIDField()
    origin_user_id=models.UUIDField()
    label=models.CharField(max_length=120,blank=True,default="")

    class Meta:
        verbose_name=_("Recovery device observation")
        verbose_name_plural=_("Recovery device observations")
        constraints=[models.UniqueConstraint(fields=["session","shop","origin_installation_id"],name="recovery_device_scope_once")]


def _json_kind(field,kind):
    return Exact(models.Func(models.F(field),function="JSON_TYPE",output_field=models.CharField()),models.Value(kind))


class RecoveryJournalDeposit(UUIDRecord):
    """Keep the current submitter and original receipt apart from integration."""
    session=models.ForeignKey(RestoreSession,on_delete=models.PROTECT,related_name="journal_deposits")
    shop=models.ForeignKey("identity.Shop",on_delete=models.PROTECT,related_name="recovery_journal_deposits")
    operation_id=models.UUIDField(unique=True)
    submitted_by=models.ForeignKey("core.User",on_delete=models.PROTECT,related_name="recovery_journal_deposits")
    submitted_installation=models.ForeignKey("identity.Installation",on_delete=models.PROTECT,related_name="recovery_journal_deposits")
    reason=models.TextField()
    format_version=models.PositiveSmallIntegerField()
    content_sha256=models.CharField(max_length=64)
    receipt=models.JSONField()

    class Meta:
        verbose_name=_("Recovery journal deposit")
        verbose_name_plural=_("Recovery journal deposits")
        ordering=["created_at","id"]
        indexes=[models.Index(fields=["session","shop","created_at","id"],name="recovery_deposit_shop_page")]
        constraints=[
            models.CheckConstraint(condition=models.Q(format_version=1)&_integer_columns("format_version"),name="recovery_deposit_version"),
            models.CheckConstraint(condition=GreaterThan(Length(Trim("reason")),models.Value(0)),name="recovery_deposit_reason"),
            models.CheckConstraint(condition=Exact(Length("content_sha256"),models.Value(64)),name="recovery_deposit_sha_length"),
            models.CheckConstraint(condition=_json_kind("receipt","object"),name="recovery_deposit_receipt_object"),
        ]


class RecoveryJournalOperation(UUIDRecord):
    """Capture a source operation without creating any business effect or right."""
    session=models.ForeignKey(RestoreSession,on_delete=models.PROTECT,related_name="journal_operations")
    shop=models.ForeignKey("identity.Shop",on_delete=models.PROTECT,related_name="recovery_journal_operations")
    first_deposit=models.ForeignKey(RecoveryJournalDeposit,on_delete=models.PROTECT,related_name="new_operations")
    operation_id=models.UUIDField()
    origin_installation_id=models.UUIDField()
    origin_user_id=models.UUIDField()
    operation_type=models.CharField(max_length=80)
    occurred_at=models.DateTimeField()
    origin_recorded_at=models.DateTimeField()
    payload=models.JSONField()
    dependencies=models.JSONField(default=list)
    content_sha256=models.CharField(max_length=64)

    class Meta:
        verbose_name=_("Captured recovery operation")
        verbose_name_plural=_("Captured recovery operations")
        ordering=["created_at","id"]
        indexes=[models.Index(fields=["session","shop","created_at","id"],name="recovery_operation_shop_page"),
            models.Index(fields=["operation_id"],name="recovery_operation_uuid")]
        constraints=[
            models.UniqueConstraint(fields=["session","operation_id"],name="recovery_operation_once"),
            models.CheckConstraint(condition=GreaterThan(Length(Trim("operation_type")),models.Value(0)),name="recovery_operation_type"),
            models.CheckConstraint(condition=Exact(Length("content_sha256"),models.Value(64)),name="recovery_operation_sha_length"),
            models.CheckConstraint(condition=_json_kind("payload","object")&_json_kind("dependencies","array"),name="recovery_operation_json_shapes"),
        ]
