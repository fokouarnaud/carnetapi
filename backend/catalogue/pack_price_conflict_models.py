"""Immutable proposals and explicit decisions for one pack's unchanged factor."""
from django.db import models
from django.db.models.functions import Length, Trim
from django.db.models.lookups import GreaterThan
from django.utils.translation import gettext_lazy as _

from common.model_base import UUIDRecord, _integer_columns


def _reason_present():
    return GreaterThan(Length(Trim("reason")), models.Value(0))


class PackPriceConflict(UUIDRecord):
    shop = models.ForeignKey("identity.Shop", on_delete=models.PROTECT, related_name="pack_price_conflicts")
    product = models.ForeignKey("catalogue.Product", on_delete=models.PROTECT, related_name="pack_price_conflicts")
    pack = models.ForeignKey("catalogue.ProductPack", on_delete=models.PROTECT, related_name="price_conflicts")
    source_change = models.OneToOneField("core.Activity", on_delete=models.PROTECT, related_name="pack_price_conflict_source")
    opening_operation = models.OneToOneField("core.Activity", on_delete=models.PROTECT, related_name="opened_pack_price_conflict")
    base_version = models.PositiveBigIntegerField()
    base_quantity_milli = models.PositiveBigIntegerField()
    common_price_fcfa = models.PositiveBigIntegerField()
    source_snapshot = models.JSONField()
    state = models.CharField(max_length=8, choices=[("open", _("Open")), ("resolved", _("Resolved"))], default="open")
    version = models.PositiveBigIntegerField(default=1)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["shop", "created_at", "id"], name="pack_price_conflict_shop_order")]
        constraints = [
            models.UniqueConstraint(fields=["pack"], condition=models.Q(state="open"), name="one_open_pack_price_conflict"),
            models.CheckConstraint(condition=models.Q(state__in=["open", "resolved"]), name="pack_price_conflict_state"),
            models.CheckConstraint(condition=models.Q(base_version__gt=0, version__gt=0, base_quantity_milli__gt=0)
                & _integer_columns("base_version", "version", "base_quantity_milli", "common_price_fcfa"), name="pack_price_conflict_int_values"),
        ]


class PackPriceProposal(UUIDRecord):
    conflict = models.ForeignKey(PackPriceConflict, on_delete=models.PROTECT, related_name="proposals")
    operation = models.OneToOneField("core.Activity", on_delete=models.PROTECT, related_name="pack_price_proposal")
    actor_id = models.UUIDField()
    installation_id = models.UUIDField(null=True, blank=True)
    is_native = models.BooleanField(default=False)
    proposed_price_fcfa = models.PositiveBigIntegerField()
    reason = models.TextField()

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["conflict"], condition=models.Q(is_native=True), name="one_native_pack_price_proposal"),
            models.CheckConstraint(condition=_integer_columns("proposed_price_fcfa"), name="pack_price_proposal_int_price"),
            models.CheckConstraint(condition=_reason_present(), name="pack_price_proposal_reason"),
        ]


class PackPriceResolution(UUIDRecord):
    conflict = models.OneToOneField(PackPriceConflict, on_delete=models.PROTECT, related_name="resolution")
    operation = models.OneToOneField("core.Activity", on_delete=models.PROTECT, related_name="pack_price_resolution")
    proposal = models.ForeignKey(PackPriceProposal, on_delete=models.PROTECT, null=True, blank=True, related_name="resolutions")
    actor_id = models.UUIDField()
    installation_id = models.UUIDField(null=True, blank=True)
    price_fcfa = models.PositiveBigIntegerField()
    reason = models.TextField()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=_integer_columns("price_fcfa"), name="pack_price_resolution_int_price"),
            models.CheckConstraint(condition=_reason_present(), name="pack_price_resolution_reason"),
        ]
