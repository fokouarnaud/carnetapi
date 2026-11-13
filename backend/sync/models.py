"""US-48: shop change feed for downstream synchronisation."""
from django.db import models

from common.model_base import _integer_columns

CHANGE_KINDS = ("upsert", "revoke", "archive")


class ShopChangeSequence(models.Model):
    """Last sequence attributed in a shop; incremented inside the writing transaction."""
    shop = models.OneToOneField("identity.Shop", on_delete=models.PROTECT, primary_key=True,
                                related_name="change_sequence")
    last_seq = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [models.CheckConstraint(condition=_integer_columns("last_seq"), name="change_sequence_integer")]


class ChangeFeedEntry(models.Model):
    """One append-only feed item; payload is serialised per reader rights at read time."""
    shop = models.ForeignKey("identity.Shop", on_delete=models.PROTECT, related_name="change_feed")
    seq = models.PositiveBigIntegerField()
    object_type = models.CharField(max_length=80)
    object_id = models.UUIDField()
    actor = models.ForeignKey("core.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    kind = models.CharField(max_length=16, choices=[(k, k) for k in CHANGE_KINDS])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["shop", "seq"], name="change_feed_shop_seq_once"),
            models.CheckConstraint(condition=models.Q(seq__gt=0) & _integer_columns("seq"), name="change_feed_positive_seq"),
            models.CheckConstraint(condition=models.Q(kind__in=CHANGE_KINDS), name="change_feed_known_kind"),
        ]
