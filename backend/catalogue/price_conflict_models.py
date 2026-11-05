"""Preserved evidence and explicit decisions for product-price conflicts."""
from django.db import models
from django.db.models.functions import Length, Trim
from django.db.models.lookups import GreaterThan
from django.utils.translation import gettext_lazy as _

from common.model_base import UUIDRecord, _integer_columns


def _reason_present():
    return GreaterThan(Length(Trim("reason")), models.Value(0))


class ProductPriceConflict(UUIDRecord):
    """A price field shares one confirmed ancestor until a manager decides."""
    shop = models.ForeignKey("identity.Shop", on_delete=models.PROTECT, related_name="price_conflicts")
    product = models.ForeignKey("catalogue.Product", on_delete=models.PROTECT, related_name="price_conflicts")
    source_change = models.OneToOneField("core.Activity", on_delete=models.PROTECT, related_name="price_conflict_source")
    opening_operation = models.OneToOneField("core.Activity", on_delete=models.PROTECT, related_name="opened_price_conflict")
    base_version = models.PositiveBigIntegerField()
    common_price_fcfa = models.PositiveBigIntegerField()
    source_snapshot = models.JSONField()
    state = models.CharField(max_length=8, choices=[("open", _("Open")), ("resolved", _("Resolved"))], default="open")
    version = models.PositiveBigIntegerField(default=1)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["shop", "created_at", "id"], name="price_conflict_shop_order")]
        constraints = [
            models.UniqueConstraint(fields=["product"], condition=models.Q(state="open"), name="one_open_product_price_conflict"),
            models.CheckConstraint(condition=models.Q(state__in=["open", "resolved"]), name="price_conflict_valid_state"),
            models.CheckConstraint(condition=models.Q(base_version__gt=0, version__gt=0)
                & _integer_columns("base_version", "version", "common_price_fcfa"), name="price_conflict_integer_values"),
        ]


class ProductPriceProposal(UUIDRecord):
    """An immutable native or received proposal keeps its original author."""
    conflict = models.ForeignKey(ProductPriceConflict, on_delete=models.PROTECT, related_name="proposals")
    operation = models.OneToOneField("core.Activity", on_delete=models.PROTECT, related_name="price_proposal")
    actor_id = models.UUIDField()
    installation_id = models.UUIDField(null=True, blank=True)
    is_native = models.BooleanField(default=False)
    proposed_price_fcfa = models.PositiveBigIntegerField()
    reason = models.TextField()

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["conflict"], condition=models.Q(is_native=True), name="one_native_price_proposal"),
            models.CheckConstraint(condition=_integer_columns("proposed_price_fcfa"), name="price_proposal_integer_price"),
            models.CheckConstraint(condition=_reason_present(), name="price_proposal_reason_required"),
        ]


class ProductPriceResolution(UUIDRecord):
    """The chosen proposal or common price is recorded without rewriting evidence."""
    conflict = models.OneToOneField(ProductPriceConflict, on_delete=models.PROTECT, related_name="resolution")
    operation = models.OneToOneField("core.Activity", on_delete=models.PROTECT, related_name="price_resolution")
    proposal = models.ForeignKey(ProductPriceProposal, on_delete=models.PROTECT, null=True, blank=True, related_name="resolutions")
    actor_id = models.UUIDField()
    installation_id = models.UUIDField(null=True, blank=True)
    price_fcfa = models.PositiveBigIntegerField()
    reason = models.TextField()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=_integer_columns("price_fcfa"), name="price_resolution_integer_price"),
            models.CheckConstraint(condition=_reason_present(), name="price_resolution_reason_required"),
        ]
