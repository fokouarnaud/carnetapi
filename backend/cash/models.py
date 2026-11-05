
from django.db import models
from django.db.models.lookups import Exact
from django.utils.translation import gettext_lazy as _
from common.model_base import UUIDRecord, _integer_columns
from sales.models import SalePayment


class CashMovement(UUIDRecord):
    """One actual money movement per declared sale payment, separate from revenue."""
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="cash_movements")
    actor = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="cash_movements")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="cash_movements")
    operation = models.ForeignKey('core.Activity', on_delete=models.PROTECT, related_name="cash_movements")
    sale_payment = models.OneToOneField('sales.SalePayment', on_delete=models.PROTECT, related_name="cash_movement", null=True, blank=True)
    customer_payment_part = models.OneToOneField('customers.CustomerPaymentPart', on_delete=models.PROTECT, related_name="cash_movement", null=True, blank=True)
    customer_refund = models.OneToOneField('customers.PaymentRegularization', on_delete=models.PROTECT, related_name="cash_movement", null=True, blank=True)
    supplier_payment_part = models.OneToOneField('purchasing.SupplierPaymentPart', on_delete=models.PROTECT, related_name="cash_movement", null=True, blank=True)
    supplier_refund = models.OneToOneField('purchasing.SupplierRefund', on_delete=models.PROTECT, related_name="cash_movement", null=True, blank=True)
    sale_return_refund = models.OneToOneField('customers.ReturnCreditSettlement', on_delete=models.PROTECT,
        null=True, blank=True, related_name="cash_movement")
    method = models.CharField(max_length=10, choices=SalePayment.Method.choices)
    manual_kind = models.CharField(max_length=12, blank=True, choices=[("opening", _("Opening cash")), ("addition", _("Cash contribution")),
                                                                    ("withdrawal", _("Withdrawal")), ("expense", _("Expense"))])
    other_name = models.CharField(max_length=80, blank=True)
    external_reference = models.CharField(max_length=150, blank=True)
    amount_fcfa = models.BigIntegerField()
    occurred_at = models.DateTimeField()
    reason = models.TextField()

    class Meta:
        db_table = "core_cashmovement"
        indexes = [models.Index(fields=["shop", "occurred_at", "id"], name="core_cashmo_shop_id_0d33dc_idx")]
        constraints = [models.CheckConstraint(condition=(((((
            (models.Q(sale_payment__isnull=True, customer_payment_part__isnull=True, customer_refund__isnull=True)
             & (models.Q(manual_kind__in=["opening", "addition"], amount_fcfa__gt=0)
                | models.Q(manual_kind__in=["withdrawal", "expense"], amount_fcfa__lt=0)))
            | (models.Q(manual_kind="") & (
                models.Q(sale_payment__isnull=False, customer_payment_part__isnull=True, customer_refund__isnull=True, amount_fcfa__gt=0)
                | models.Q(sale_payment__isnull=True, customer_payment_part__isnull=False, customer_refund__isnull=True, amount_fcfa__gt=0)
                | models.Q(sale_payment__isnull=True, customer_payment_part__isnull=True, customer_refund__isnull=False, amount_fcfa__lt=0)
            ))
        ) & models.Q(supplier_payment_part__isnull=True)) | models.Q(
            manual_kind="", sale_payment__isnull=True, customer_payment_part__isnull=True,
            customer_refund__isnull=True, supplier_payment_part__isnull=False, amount_fcfa__lt=0,
        )) & models.Q(supplier_refund__isnull=True)) | models.Q(
            manual_kind="", sale_payment__isnull=True, customer_payment_part__isnull=True,
            customer_refund__isnull=True, supplier_payment_part__isnull=True, supplier_refund__isnull=False, amount_fcfa__gt=0,
        )) & models.Q(sale_return_refund__isnull=True) | models.Q(
            sale_return_refund__isnull=False, sale_payment__isnull=True, customer_payment_part__isnull=True,
            customer_refund__isnull=True, supplier_payment_part__isnull=True, supplier_refund__isnull=True,
            manual_kind="", amount_fcfa__lt=0,
        ), name="cash_event_source_sign"),
            models.CheckConstraint(condition=_integer_columns("amount_fcfa"), name="cash_movement_integral_amount")]


class CashCount(UUIDRecord):
    """An immutable physical observation with its original exact expected balance."""
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="cash_counts")
    actor = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="cash_counts")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="cash_counts")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="cash_count")
    method = models.CharField(max_length=10, choices=SalePayment.Method.choices)
    other_name = models.CharField(max_length=80, blank=True)
    physical_fcfa = models.PositiveBigIntegerField()
    # JSON stores an exact integer literal even when an aggregate exceeds int64.
    expected_at_creation_fcfa = models.JSONField()
    counted_at = models.DateTimeField()
    reason = models.CharField(max_length=200)

    class Meta:
        db_table = "core_cashcount"
        indexes = [models.Index(fields=["shop", "counted_at", "id"], name="core_cashco_shop_id_7d1ff0_idx")]
        constraints = [models.CheckConstraint(condition=_integer_columns("physical_fcfa"), name="cash_count_integral_physical"),
            models.CheckConstraint(
            condition=Exact(models.Func(models.F("expected_at_creation_fcfa"), function="JSON_TYPE", output_field=models.CharField()),
                            models.Value("integer")), name="cash_count_expected_integer",
        )]
