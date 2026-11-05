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
