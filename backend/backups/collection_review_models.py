"""Keep recovered collection money apart until a current business decision."""
from django.db import models
from django.db.models.functions import Length,Trim
from django.db.models.lookups import GreaterThan
from django.utils.translation import gettext_lazy as _

from common.model_base import UUIDRecord,_integer_columns
from backups.recovery_models import _json_kind


class RecoveryCollectionReview(UUIDRecord):
    capture=models.OneToOneField("backups.RecoveryJournalOperation",on_delete=models.PROTECT,related_name="collection_review")
    source_operation_id=models.UUIDField(unique=True)
    payment_id=models.UUIDField(unique=True)
    customer_id=models.UUIDField()
    amount_fcfa=models.PositiveBigIntegerField()
    original_applied_fcfa=models.PositiveBigIntegerField()
    original_unallocated_fcfa=models.PositiveBigIntegerField()
    state=models.CharField(max_length=12,default="pending")
    version=models.PositiveBigIntegerField(default=0)

    class Meta:
        ordering=["created_at","id"]
        verbose_name=_("Recovered collection awaiting review")
        verbose_name_plural=_("Recovered collections awaiting review")
        indexes=[models.Index(fields=["state","created_at","id"],name="recovery_collection_state_page")]
        constraints=[
            models.CheckConstraint(condition=models.Q(state__in=["pending","refused","validated"]),name="recovery_collection_state"),
            models.CheckConstraint(condition=models.Q(amount_fcfa__gt=0,original_applied_fcfa__lte=models.F("amount_fcfa"),
                original_unallocated_fcfa=models.F("amount_fcfa")-models.F("original_applied_fcfa")),name="recovery_collection_money"),
            models.CheckConstraint(condition=_integer_columns("version","amount_fcfa","original_applied_fcfa","original_unallocated_fcfa"),
                name="recovery_collection_integers"),
        ]


class RecoveryCollectionReviewDecision(UUIDRecord):
    review=models.ForeignKey("backups.RecoveryCollectionReview",on_delete=models.PROTECT,related_name="decisions")
    capture=models.ForeignKey("backups.RecoveryJournalOperation",on_delete=models.PROTECT,related_name="collection_review_decisions")
    operation=models.OneToOneField("core.Activity",on_delete=models.PROTECT,related_name="recovery_collection_review_decision")
    reviewed_by=models.ForeignKey("core.User",on_delete=models.PROTECT,related_name="recovery_collection_review_decisions")
    reviewed_installation=models.ForeignKey("identity.Installation",on_delete=models.PROTECT,related_name="recovery_collection_review_decisions")
    previous_version=models.PositiveBigIntegerField()
    version=models.PositiveBigIntegerField()
    state=models.CharField(max_length=12)
    reason=models.TextField()
    request=models.JSONField()
    receipt=models.JSONField()
    integration_decision=models.OneToOneField("backups.RecoveryIntegrationDecision",on_delete=models.PROTECT,
        null=True,blank=True,related_name="collection_review_decision")

    class Meta:
        ordering=["version","id"]
        verbose_name=_("Recovered collection review decision")
        verbose_name_plural=_("Recovered collection review decisions")
        constraints=[
            models.UniqueConstraint(fields=["review","version"],name="recovery_collection_version_once"),
            models.CheckConstraint(condition=models.Q(previous_version__gte=0,version=models.F("previous_version")+1)
                &_integer_columns("previous_version","version"),name="recovery_collection_versions"),
            models.CheckConstraint(condition=models.Q(state__in=["pending","refused"],integration_decision__isnull=True)
                |models.Q(state="validated",integration_decision__isnull=False),name="recovery_collection_effect_link"),
            models.CheckConstraint(condition=GreaterThan(Length(Trim("reason")),models.Value(0)),name="recovery_collection_reason"),
            models.CheckConstraint(condition=_json_kind("request","object")&_json_kind("receipt","object"),name="recovery_collection_json"),
        ]
