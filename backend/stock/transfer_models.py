"""Physical shipments and receipts keep independent catalogues and immutable costs."""
from django.db import models
from django.db.models.functions import Cast
from django.db.models.lookups import Exact, GreaterThanOrEqual
from django.utils.translation import gettext_lazy as _
from common.model_base import UUIDRecord, _integer_columns


def quantity_rule():
    return (models.Q(quantity_milli__gt=0) & _integer_columns("quantity_milli")
            & models.Q(unit_kind__in=["piece","weight","volume"])
            & (~models.Q(unit_kind="piece") | Exact(models.F("quantity_milli") % 1000,models.Value(0))))


class StockTransfer(UUIDRecord):
    source_shop=models.ForeignKey("identity.Shop",on_delete=models.PROTECT,related_name="outgoing_transfers")
    destination_shop=models.ForeignKey("identity.Shop",on_delete=models.PROTECT,related_name="incoming_transfers")
    operation=models.OneToOneField("core.Activity",on_delete=models.PROTECT,related_name="stock_transfer")
    installation=models.ForeignKey("identity.Installation",on_delete=models.PROTECT,related_name="departed_transfers")
    occurred_at=models.DateTimeField()

    class Meta:
        verbose_name=_("Stock transfer")
        verbose_name_plural=_("Stock transfers")
        indexes=[models.Index(fields=["source_shop","occurred_at","id"],name="transfer_source_page"),
                 models.Index(fields=["destination_shop","occurred_at","id"],name="transfer_destination_page")]
        constraints=[models.CheckConstraint(condition=~models.Q(source_shop=models.F("destination_shop")),name="transfer_different_shops")]


class StockTransferLine(UUIDRecord):
    transfer=models.ForeignKey(StockTransfer,on_delete=models.PROTECT,related_name="lines")
    product=models.ForeignKey("catalogue.Product",on_delete=models.PROTECT,related_name="transfer_departures")
    product_version=models.PositiveBigIntegerField()
    product_name=models.CharField(max_length=150)
    unit_kind=models.CharField(max_length=10)
    unit_label=models.CharField(max_length=40)
    quantity_milli=models.PositiveBigIntegerField()
    movement=models.OneToOneField("stock.StockMovement",on_delete=models.PROTECT,related_name="transfer_departure_line")
    source_cost=models.OneToOneField("stock.StockCostSnapshot",on_delete=models.PROTECT,related_name="transfer_origin")

    class Meta:
        verbose_name=_("Stock transfer line")
        verbose_name_plural=_("Stock transfer lines")
        ordering=["created_at","id"]
        constraints=[models.CheckConstraint(condition=quantity_rule(),name="transfer_line_quantity"),
            models.CheckConstraint(condition=models.Q(product_version__gt=0)&_integer_columns("product_version"),name="transfer_line_version")]


class StockTransferReceipt(UUIDRecord):
    transfer=models.ForeignKey(StockTransfer,on_delete=models.PROTECT,related_name="receipts")
    operation=models.OneToOneField("core.Activity",on_delete=models.PROTECT,related_name="transfer_receipt")
    installation=models.ForeignKey("identity.Installation",on_delete=models.PROTECT,related_name="received_transfers")
    occurred_at=models.DateTimeField()

    class Meta:
        verbose_name=_("Stock transfer receipt")
        verbose_name_plural=_("Stock transfer receipts")
        ordering=["occurred_at","id"]


class StockTransferReceiptLine(UUIDRecord):
    receipt=models.ForeignKey(StockTransferReceipt,on_delete=models.PROTECT,related_name="lines")
    transfer_line=models.ForeignKey(StockTransferLine,on_delete=models.PROTECT,related_name="receipt_lines")
    product=models.ForeignKey("catalogue.Product",on_delete=models.PROTECT,related_name="transfer_arrivals")
    product_version=models.PositiveBigIntegerField()
    product_name=models.CharField(max_length=150)
    unit_kind=models.CharField(max_length=10)
    unit_label=models.CharField(max_length=40)
    quantity_milli=models.PositiveBigIntegerField()
    received_before_milli=models.PositiveBigIntegerField()
    cost_state=models.CharField(max_length=12)
    total_cost_nanos_fcfa=models.JSONField(null=True,blank=True)
    movement=models.OneToOneField("stock.StockMovement",on_delete=models.PROTECT,related_name="transfer_receipt_line")

    class Meta:
        verbose_name=_("Stock transfer receipt line")
        verbose_name_plural=_("Stock transfer receipt lines")
        ordering=["created_at","id"]
        constraints=[models.CheckConstraint(condition=quantity_rule()&models.Q(received_before_milli__gte=0)
            &_integer_columns("received_before_milli","product_version")&models.Q(product_version__gt=0),name="transfer_receipt_line_quantity"),
            models.CheckConstraint(condition=models.Q(cost_state="unknown",total_cost_nanos_fcfa__isnull=True) |
                (models.Q(cost_state__in=["known","provisional"],total_cost_nanos_fcfa__isnull=False)
                 & Exact(models.Func(models.F("total_cost_nanos_fcfa"),function="JSON_TYPE",output_field=models.CharField()),models.Value("integer"))
                 & GreaterThanOrEqual(Cast("total_cost_nanos_fcfa",models.BigIntegerField()),models.Value(0))),name="transfer_receipt_exact_cost")]
