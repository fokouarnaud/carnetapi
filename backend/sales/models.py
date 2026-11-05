
from django.db import models
from django.db.models.lookups import Exact
from django.utils.translation import gettext_lazy as _
from common.model_base import UUIDRecord, _integer_columns


class Sale(UUIDRecord):
    mode = models.CharField(max_length=10, choices=[("cash", _("Cash sale")), ("credit", _("Credit sale"))], default="cash")
    customer = models.ForeignKey('customers.Customer', on_delete=models.PROTECT, related_name="sales", null=True, blank=True)
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="sales")
    actor = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="sales")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="sales")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="sale")
    occurred_at = models.DateTimeField()
    reference_total_fcfa = models.PositiveBigIntegerField()
    discount_total_fcfa = models.PositiveBigIntegerField()
    net_total_fcfa = models.PositiveBigIntegerField()
    stock_requires_regularization = models.BooleanField(default=False)

    class Meta:
        db_table = "core_sale"
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["shop", "created_at", "id"], name="core_sale_shop_id_9f8a6e_idx")]
        constraints = [
            models.CheckConstraint(condition=(models.Q(mode="cash") | models.Q(mode="credit", customer__isnull=False)),
                                   name="sale_identified_credit_customer"),
            models.CheckConstraint(condition=_integer_columns("reference_total_fcfa", "discount_total_fcfa", "net_total_fcfa"),
                                   name="sale_integral_totals"),
            models.CheckConstraint(condition=models.Q(discount_total_fcfa__lte=models.F("reference_total_fcfa")), name="sale_discount_within_total"),
            models.CheckConstraint(condition=models.Q(net_total_fcfa=models.F("reference_total_fcfa") - models.F("discount_total_fcfa")), name="sale_exact_net_total"),
        ]


class SaleLine(UUIDRecord):
    """Invoice quantities, factors and tariffs are snapshots, never live properties."""
    sale = models.ForeignKey('sales.Sale', on_delete=models.PROTECT, related_name="lines")
    product = models.ForeignKey('catalogue.Product', on_delete=models.PROTECT, related_name="sale_lines")
    pack = models.ForeignKey('catalogue.ProductPack', on_delete=models.PROTECT, related_name="sale_lines", null=True, blank=True)
    position = models.PositiveIntegerField()
    product_version = models.PositiveBigIntegerField()
    product_name = models.CharField(max_length=150)
    unit_kind = models.CharField(max_length=10)
    unit_label = models.CharField(max_length=80)
    quantity_milli = models.PositiveBigIntegerField()
    factor_milli = models.PositiveBigIntegerField()
    base_quantity_milli = models.PositiveBigIntegerField()
    unit_price_fcfa = models.PositiveBigIntegerField()
    reference_fcfa = models.PositiveBigIntegerField()
    own_discount_fcfa = models.PositiveBigIntegerField()
    global_discount_fcfa = models.PositiveBigIntegerField()
    net_fcfa = models.PositiveBigIntegerField()
    unit_cost_micros = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "core_saleline"
        ordering = ["position", "id"]
        constraints = [
            models.CheckConstraint(condition=_integer_columns("quantity_milli", "factor_milli", "base_quantity_milli", "product_version", "position"),
                                   name="sale_line_integral_quantities"),
            models.CheckConstraint(condition=models.Q(unit_kind__in=["piece", "weight", "volume"])
                & (~models.Q(unit_kind="piece") | (Exact(models.F("quantity_milli") % 1000, models.Value(0))
                                                   & Exact(models.F("base_quantity_milli") % 1000, models.Value(0)))),
                name="sale_line_piece_precision"),
            models.CheckConstraint(condition=_integer_columns("unit_price_fcfa", "reference_fcfa", "own_discount_fcfa", "global_discount_fcfa", "net_fcfa")
                & (models.Q(unit_cost_micros__isnull=True) | _integer_columns("unit_cost_micros")),
                name="sale_line_integral_money"),
            models.UniqueConstraint(fields=["sale", "position"], name="sale_line_position_unique"),
            models.CheckConstraint(condition=models.Q(quantity_milli__gt=0, factor_milli__gt=0, base_quantity_milli__gt=0), name="sale_line_positive_quantities"),
            models.CheckConstraint(condition=models.Q(own_discount_fcfa__lte=models.F("reference_fcfa")), name="sale_line_own_discount_bound"),
            models.CheckConstraint(condition=models.Q(global_discount_fcfa__lte=models.F("reference_fcfa") - models.F("own_discount_fcfa")), name="sale_line_global_discount_bound"),
            models.CheckConstraint(condition=models.Q(net_fcfa=models.F("reference_fcfa") - models.F("own_discount_fcfa") - models.F("global_discount_fcfa")), name="sale_line_exact_net"),
        ]


class SalePayment(UUIDRecord):
    class Method(models.TextChoices):
        CASH = "cash", _("Cash")
        MTN = "mtn", "MTN"
        ORANGE = "orange", "Orange"
        BANK = "bank", _("Bank transfer")
        OTHER = "other", _("Other")

    sale = models.ForeignKey('sales.Sale', on_delete=models.PROTECT, related_name="payments")
    position = models.PositiveIntegerField()
    method = models.CharField(max_length=10, choices=Method.choices)
    other_name = models.CharField(max_length=80, blank=True)
    amount_fcfa = models.PositiveBigIntegerField()
    external_reference = models.CharField(max_length=150, blank=True)
    received_fcfa = models.PositiveBigIntegerField(null=True, blank=True)
    returned_fcfa = models.PositiveBigIntegerField(null=True, blank=True)
    possible_duplicate_reference = models.BooleanField(default=False)

    class Meta:
        db_table = "core_salepayment"
        ordering = ["position", "id"]
        indexes = [models.Index(fields=["external_reference", "method"], name="core_salepa_externa_1d92dd_idx")]
        constraints = [
            models.CheckConstraint(condition=_integer_columns("position", "amount_fcfa")
                & (models.Q(received_fcfa__isnull=True) | _integer_columns("received_fcfa"))
                & (models.Q(returned_fcfa__isnull=True) | _integer_columns("returned_fcfa")),
                name="sale_payment_integral_money"),
            models.UniqueConstraint(fields=["sale", "position"], name="sale_payment_position_unique"),
            models.CheckConstraint(condition=(
                models.Q(received_fcfa__isnull=True, returned_fcfa__isnull=True)
                | (models.Q(received_fcfa__isnull=False, returned_fcfa__isnull=False)
                   & models.Q(received_fcfa=models.F("amount_fcfa") + models.F("returned_fcfa")))
            ), name="sale_payment_change_exact"),
        ]


class SaleReturn(UUIDRecord):
    """A commercial return retains its original sale and a distinct audit event."""
    amount_fcfa = models.PositiveBigIntegerField(default=0)
    debt_reduced_fcfa = models.PositiveBigIntegerField(default=0)
    credit_fcfa = models.PositiveBigIntegerField(default=0)
    sale = models.ForeignKey('sales.Sale', on_delete=models.PROTECT, related_name="returns")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="sale_return")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="sale_returns")
    occurred_at = models.DateTimeField()

    class Meta:
        db_table = "core_salereturn"
        ordering = ["occurred_at", "id"]
        indexes = [models.Index(fields=["sale", "occurred_at", "id"], name="core_salere_sale_id_1b3e2a_idx")]
        constraints = [
            models.CheckConstraint(condition=_integer_columns("amount_fcfa", "debt_reduced_fcfa", "credit_fcfa"), name="sale_return_integer_amounts"),
            models.CheckConstraint(condition=models.Q(debt_reduced_fcfa__lte=models.F("amount_fcfa"),
                credit_fcfa=models.F("amount_fcfa") - models.F("debt_reduced_fcfa")), name="sale_return_exact_obligation"),
        ]


class SaleReturnLine(UUIDRecord):
    """Returned base quantities and net value refer to the invoiced line."""
    sale_return = models.ForeignKey('sales.SaleReturn', on_delete=models.PROTECT, related_name="lines")
    sale_line = models.ForeignKey('sales.SaleLine', on_delete=models.PROTECT, related_name="return_lines")
    quantity_milli = models.PositiveBigIntegerField()
    amount_fcfa = models.PositiveBigIntegerField()
    original_quantity_milli = models.PositiveBigIntegerField()
    original_amount_fcfa = models.PositiveBigIntegerField()
    unit_kind = models.CharField(max_length=10)

    class Meta:
        db_table = "core_salereturnline"
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["sale_return", "sale_line"], name="return_original_line_once"),
            models.CheckConstraint(condition=_integer_columns("quantity_milli", "amount_fcfa", "original_quantity_milli", "original_amount_fcfa"), name="return_line_exact_integers"),
            models.CheckConstraint(condition=models.Q(quantity_milli__gt=0, original_quantity_milli__gt=0,
                quantity_milli__lte=models.F("original_quantity_milli"), amount_fcfa__lte=models.F("original_amount_fcfa")), name="return_line_snapshot_bounds"),
            models.CheckConstraint(condition=models.Q(unit_kind__in=["piece", "weight", "volume"]) &
                (~models.Q(unit_kind="piece") | Exact(models.F("quantity_milli") % 1000, models.Value(0))), name="return_line_unit_precision"),
        ]


class SaleCancellation(UUIDRecord):
    """Cancellation state is an append-only event, preserving the invoice and cash."""
    sale = models.OneToOneField('sales.Sale', on_delete=models.PROTECT, related_name="cancellation")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="sale_cancellation")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="sale_cancellations")
    sale_return = models.OneToOneField('sales.SaleReturn', on_delete=models.PROTECT, related_name="cancellation_origin", null=True, blank=True)
    occurred_at = models.DateTimeField()

    class Meta:
        db_table = "core_salecancellation"
