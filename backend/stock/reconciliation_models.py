"""Explicit links release already accounted departures without changing stock events."""
from django.db import models
from django.utils.translation import gettext_lazy as _
from common.model_base import UUIDRecord, _integer_columns


class StockReservationReconciliation(UUIDRecord):
    reservation=models.ForeignKey("stock.StockReservation",on_delete=models.PROTECT,related_name="reconciliations")
    movement=models.ForeignKey("stock.StockMovement",on_delete=models.PROTECT,related_name="reservation_reconciliations")
    operation=models.OneToOneField("core.Activity",on_delete=models.PROTECT,related_name="reservation_reconciliation")
    installation=models.ForeignKey("identity.Installation",on_delete=models.PROTECT,related_name="reservation_reconciliations")
    quantity_milli=models.PositiveBigIntegerField()

    class Meta:
        ordering=["created_at","id"]
        verbose_name=_("Stock reservation reconciliation")
        verbose_name_plural=_("Stock reservation reconciliations")
        constraints=[models.CheckConstraint(condition=models.Q(quantity_milli__gt=0)&_integer_columns("quantity_milli"),
            name="reservation_reconciliation_qty")]


class StockReservationPresenceRelease(UUIDRecord):
    """G19: a refused sale whose goods are still present frees its reservation; no movement is created."""
    reservation=models.ForeignKey("stock.StockReservation",on_delete=models.PROTECT,related_name="presence_releases")
    operation=models.OneToOneField("core.Activity",on_delete=models.PROTECT,related_name="reservation_presence_release")
    installation=models.ForeignKey("identity.Installation",on_delete=models.PROTECT,related_name="reservation_presence_releases")
    quantity_milli=models.PositiveBigIntegerField()
    reason=models.TextField()

    class Meta:
        ordering=["created_at","id"]
        constraints=[models.CheckConstraint(condition=models.Q(quantity_milli__gt=0)&_integer_columns("quantity_milli"),
            name="reservation_presence_release_qty"),
            models.CheckConstraint(condition=~models.Q(reason=""),name="reservation_presence_release_reason")]
