import uuid

from django.core.validators import RegexValidator
from django.db import models
from django.db.models.lookups import Exact
from django.utils.translation import gettext_lazy as _
from common.model_base import UUIDRecord, _integer_columns, _scaled_integer_product
from sales.models import SalePayment


class Supplier(UUIDRecord):
    """A shop's business counterparty, independent of authentication accounts."""
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="suppliers")
    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=16, null=True, blank=True, validators=[RegexValidator(r"^\+[1-9][0-9]{7,14}$")])
    version = models.PositiveBigIntegerField(default=1)

    class Meta:
        db_table = "core_supplier"
        constraints = [models.CheckConstraint(condition=models.Q(version__gt=0) & _integer_columns("version"),
                                             name="supplier_positive_integer_version")]
        indexes = [models.Index(fields=["shop", "created_at", "id"], name="core_suppli_shop_id_dd8254_idx")]


class PurchaseDocument(UUIDRecord):
    """An order forecasts goods; an invoice records an actual obligation."""
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="purchase_documents")
    supplier = models.ForeignKey('purchasing.Supplier', on_delete=models.PROTECT, related_name="documents")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="purchase_document")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT)
    kind = models.CharField(max_length=10, choices=[("order", _("Order")), ("invoice", _("Invoice"))])
    order = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="invoices")
    occurred_at = models.DateTimeField()
    external_reference = models.CharField(max_length=150, blank=True)
    total_fcfa = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_purchasedocument"
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["shop", "supplier", "created_at", "id"], name="core_purcha_shop_id_272369_idx")]
        constraints = [models.CheckConstraint(condition=models.Q(kind__in=["order", "invoice"]), name="purchase_document_kind"),
            models.CheckConstraint(condition=_integer_columns("total_fcfa"), name="purchase_document_integral_total")]


class PurchaseLine(UUIDRecord):
    document = models.ForeignKey('purchasing.PurchaseDocument', on_delete=models.PROTECT, related_name="lines")
    product = models.ForeignKey('catalogue.Product', on_delete=models.PROTECT)
    pack = models.ForeignKey('catalogue.ProductPack', on_delete=models.PROTECT, null=True, blank=True)
    position = models.PositiveIntegerField()
    product_name = models.CharField(max_length=150)
    unit_label = models.CharField(max_length=80)
    quantity_milli = models.PositiveBigIntegerField()
    factor_milli = models.PositiveBigIntegerField()
    base_quantity_milli = models.PositiveBigIntegerField()
    unit_price_fcfa = models.PositiveBigIntegerField()
    total_fcfa = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_purchaseline"
        ordering = ["position", "id"]
        constraints = [
            models.CheckConstraint(condition=_integer_columns("position", "quantity_milli", "factor_milli", "base_quantity_milli", "unit_price_fcfa", "total_fcfa"),
                                   name="purchase_line_integral_values"),
            models.UniqueConstraint(fields=["document", "position"], name="purchase_line_position"),
            models.CheckConstraint(condition=models.Q(quantity_milli__gt=0, factor_milli__gt=0, base_quantity_milli__gt=0), name="purchase_line_positive_quantities"),
            models.CheckConstraint(condition=models.Q(base_quantity_milli=_scaled_integer_product("quantity_milli", "factor_milli")), name="purchase_line_exact_base"),
            models.CheckConstraint(condition=Exact(((models.F("quantity_milli") % 1000) * (models.F("factor_milli") % 1000)) % 1000, models.Value(0)), name="purchase_line_base_precision"),
            models.CheckConstraint(condition=models.Q(total_fcfa=_scaled_integer_product("quantity_milli", "unit_price_fcfa", 500)), name="purchase_line_exact_total"),
        ]


class SupplierDebt(UUIDRecord):
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="supplier_debts")
    supplier = models.ForeignKey('purchasing.Supplier', on_delete=models.PROTECT, related_name="debts")
    document = models.OneToOneField('purchasing.PurchaseDocument', on_delete=models.PROTECT, null=True, blank=True, related_name="debt")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="supplier_debt")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT)
    occurred_at = models.DateTimeField()
    amount_fcfa = models.PositiveBigIntegerField()
    remaining_fcfa = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_supplierdebt"
        ordering = ["occurred_at", "id"]
        indexes = [models.Index(fields=["shop", "supplier", "occurred_at", "id"], name="core_suppli_shop_id_d7328d_idx")]
        constraints = [models.CheckConstraint(condition=models.Q(remaining_fcfa__lte=models.F("amount_fcfa")), name="supplier_debt_remaining_bound"),
            models.CheckConstraint(condition=_integer_columns("amount_fcfa", "remaining_fcfa"), name="supplier_debt_integral_amounts")]


class PurchaseReceipt(UUIDRecord):
    """A confirmed physical arrival, independent of debt and money."""
    document = models.ForeignKey('purchasing.PurchaseDocument', on_delete=models.PROTECT, related_name="receipts")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="purchase_receipt")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT)
    occurred_at = models.DateTimeField()

    class Meta:
        db_table = "core_purchasereceipt"
        ordering = ["created_at", "id"]


class PurchaseReceiptLine(UUIDRecord):
    receipt = models.ForeignKey('purchasing.PurchaseReceipt', on_delete=models.PROTECT, related_name="lines")
    line = models.ForeignKey('purchasing.PurchaseLine', on_delete=models.PROTECT, related_name="receipt_lines")
    movement = models.OneToOneField('stock.StockMovement', on_delete=models.PROTECT, related_name="purchase_receipt_line")
    quantity_milli = models.PositiveBigIntegerField()
    base_quantity_milli = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_purchasereceiptline"
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=_integer_columns("quantity_milli", "base_quantity_milli"),
                                   name="purchase_receipt_integral_qty"),
            models.UniqueConstraint(fields=["receipt", "line"], name="receipt_purchase_line_once"),
            models.CheckConstraint(condition=models.Q(quantity_milli__gt=0, base_quantity_milli__gt=0), name="receipt_positive_quantities"),
        ]


class SupplierPayment(UUIDRecord):
    """Actual money paid, whose remaining advance is allocated explicitly."""
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="supplier_payments")
    supplier = models.ForeignKey('purchasing.Supplier', on_delete=models.PROTECT, related_name="payments")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="supplier_payment")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT)
    occurred_at = models.DateTimeField()
    amount_fcfa = models.PositiveBigIntegerField()
    unallocated_fcfa = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_supplierpayment"
        ordering = ["occurred_at", "id"]
        indexes = [models.Index(fields=["shop", "supplier", "occurred_at", "id"], name="core_suppli_shop_id_2d87fa_idx")]
        constraints = [
            models.CheckConstraint(condition=_integer_columns("amount_fcfa", "unallocated_fcfa"),
                                   name="supplier_payment_integral_money"),
            models.CheckConstraint(condition=models.Q(amount_fcfa__gt=0), name="supplier_payment_positive"),
            models.CheckConstraint(condition=models.Q(unallocated_fcfa__lte=models.F("amount_fcfa")), name="supplier_payment_available_bound"),
        ]


class SupplierPaymentPart(UUIDRecord):
    payment = models.ForeignKey('purchasing.SupplierPayment', on_delete=models.PROTECT, related_name="parts")
    position = models.PositiveIntegerField()
    method = models.CharField(max_length=10, choices=SalePayment.Method.choices)
    other_name = models.CharField(max_length=80, blank=True)
    external_reference = models.CharField(max_length=150, blank=True)
    amount_fcfa = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_supplierpaymentpart"
        ordering = ["position", "id"]
        constraints = [
            models.CheckConstraint(condition=_integer_columns("position", "amount_fcfa"),
                                   name="supplier_payment_part_integers"),
            models.UniqueConstraint(fields=["payment", "position"], name="supplier_payment_part_position"),
            models.CheckConstraint(condition=models.Q(amount_fcfa__gt=0), name="supplier_payment_part_positive"),
            models.CheckConstraint(condition=(
                models.Q(method__in=["cash", "mtn", "orange", "bank"], other_name="")
                | (models.Q(method="other") & ~models.Q(other_name=""))
            ), name="supplier_payment_named_method"),
        ]


class SupplierPaymentAllocation(UUIDRecord):
    """A noncash application of previously paid money to one supplier debt."""
    payment = models.ForeignKey('purchasing.SupplierPayment', on_delete=models.PROTECT, related_name="allocations")
    operation = models.ForeignKey('core.Activity', on_delete=models.PROTECT, related_name="supplier_allocations")
    debt = models.ForeignKey('purchasing.SupplierDebt', on_delete=models.PROTECT, related_name="allocations")
    position = models.PositiveIntegerField()
    amount_fcfa = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_supplierpaymentallocation"
        ordering = ["operation__created_at", "position", "id"]
        constraints = [
            models.CheckConstraint(condition=_integer_columns("position", "amount_fcfa"),
                                   name="supplier_allocation_integers"),
            models.UniqueConstraint(fields=["operation", "position"], name="supplier_allocation_position"),
            models.UniqueConstraint(fields=["operation", "debt"], name="supplier_allocation_debt_once"),
            models.CheckConstraint(condition=models.Q(amount_fcfa__gt=0), name="supplier_allocation_positive"),
        ]


class SupplierReturn(UUIDRecord):
    """Physical departure only: accepted credit and money are separate facts."""
    document = models.ForeignKey('purchasing.PurchaseDocument', on_delete=models.PROTECT, related_name="returns")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="supplier_return")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT)
    occurred_at = models.DateTimeField()

    class Meta:
        db_table = "core_supplierreturn"
        ordering = ["created_at", "id"]


class SupplierReturnLine(UUIDRecord):
    supplier_return = models.ForeignKey('purchasing.SupplierReturn', on_delete=models.PROTECT, related_name="lines")
    line = models.ForeignKey('purchasing.PurchaseLine', on_delete=models.PROTECT, related_name="return_lines")
    movement = models.OneToOneField('stock.StockMovement', on_delete=models.PROTECT, related_name="supplier_return_line")
    quantity_milli = models.PositiveBigIntegerField()
    base_quantity_milli = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_supplierreturnline"
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=_integer_columns("quantity_milli", "base_quantity_milli"),
                                   name="supplier_return_integral_qty"),
            models.UniqueConstraint(fields=["supplier_return", "line"], name="supplier_return_line_once"),
            models.CheckConstraint(condition=models.Q(quantity_milli__gt=0, base_quantity_milli__gt=0), name="supplier_return_positive_quantities"),
        ]


class SupplierCredit(UUIDRecord):
    """An accepted credit's excess remains a supplier claim, not cash received."""
    class Kind(models.TextChoices):
        RETURN = "return_credit", _("Return credit")
        OPENING_ADVANCE = "opening_advance", _("Opening advance")
        OPENING_CREDIT = "opening_credit", _("Opening credit")

    kind = models.CharField(max_length=24, choices=Kind.choices, default=Kind.RETURN)
    opening_shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="supplier_opening_credits", null=True, blank=True)
    opening_supplier = models.ForeignKey('purchasing.Supplier', on_delete=models.PROTECT, related_name="opening_credits", null=True, blank=True)
    supplier_return = models.ForeignKey('purchasing.SupplierReturn', on_delete=models.PROTECT, related_name="credits", null=True, blank=True)
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="supplier_credit")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT)
    occurred_at = models.DateTimeField()
    reference_fcfa = models.PositiveBigIntegerField(null=True, blank=True)
    amount_fcfa = models.PositiveBigIntegerField()
    applied_to_origin_fcfa = models.PositiveBigIntegerField()
    remaining_fcfa = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_suppliercredit"
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=(
                models.Q(kind="return_credit", supplier_return__isnull=False, opening_shop__isnull=True,
                         opening_supplier__isnull=True, reference_fcfa__isnull=False)
                | models.Q(kind__in=["opening_advance", "opening_credit"], supplier_return__isnull=True,
                           opening_shop__isnull=False, opening_supplier__isnull=False, reference_fcfa__isnull=True,
                           applied_to_origin_fcfa=0)), name="supplier_credit_explicit_origin"),
            models.CheckConstraint(condition=models.Q(amount_fcfa__gt=0), name="supplier_credit_positive"),
            models.CheckConstraint(condition=models.Q(applied_to_origin_fcfa__lte=models.F("amount_fcfa")), name="supplier_credit_origin_bound"),
            models.CheckConstraint(condition=models.Q(remaining_fcfa__lte=models.F("amount_fcfa") - models.F("applied_to_origin_fcfa")), name="supplier_credit_remaining_bound"),
            models.CheckConstraint(condition=(models.Q(reference_fcfa__isnull=True) | _integer_columns("reference_fcfa")) & _integer_columns("amount_fcfa", "applied_to_origin_fcfa", "remaining_fcfa"), name="supplier_credit_integer_amounts"),
        ]


class SupplierRefund(UUIDRecord):
    """A real receipt of money consumes one supplier credit without changing stock."""
    credit = models.ForeignKey('purchasing.SupplierCredit', on_delete=models.PROTECT, related_name="refunds")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="supplier_refund")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT)
    occurred_at = models.DateTimeField()
    method = models.CharField(max_length=10, choices=SalePayment.Method.choices)
    other_name = models.CharField(max_length=80, blank=True)
    external_reference = models.CharField(max_length=150, blank=True)
    amount_fcfa = models.PositiveBigIntegerField()
    after_remaining_fcfa = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_supplierrefund"
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount_fcfa__gt=0), name="supplier_refund_positive"),
            models.CheckConstraint(condition=_integer_columns("amount_fcfa", "after_remaining_fcfa"), name="supplier_refund_integer_amounts"),
            models.CheckConstraint(condition=(models.Q(method__in=["cash", "mtn", "orange", "bank"], other_name="")
                                               | (models.Q(method="other") & ~models.Q(other_name=""))), name="supplier_refund_named_method"),
        ]


class SupplierCreditAllocation(UUIDRecord):
    """An explicit noncash use of excess supplier credit on another obligation."""
    credit = models.ForeignKey('purchasing.SupplierCredit', on_delete=models.PROTECT, related_name="allocations")
    operation = models.ForeignKey('core.Activity', on_delete=models.PROTECT, related_name="supplier_credit_allocations")
    debt = models.ForeignKey('purchasing.SupplierDebt', on_delete=models.PROTECT, related_name="credit_allocations")
    position = models.PositiveIntegerField()
    amount_fcfa = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_suppliercreditallocation"
        ordering = ["operation__created_at", "position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["operation", "position"], name="supplier_credit_allocation_pos"),
            models.UniqueConstraint(fields=["operation", "debt"], name="supplier_credit_allocation_debt"),
            models.CheckConstraint(condition=models.Q(amount_fcfa__gt=0) & _integer_columns("amount_fcfa"), name="supplier_credit_alloc_integer"),
        ]


class SupplierLedgerEntry(models.Model):
    """Immutable supplier account effects, distinct from stock and cash movements."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="supplier_ledger")
    supplier = models.ForeignKey('purchasing.Supplier', on_delete=models.PROTECT, related_name="ledger_entries")
    operation = models.ForeignKey('core.Activity', on_delete=models.PROTECT, related_name="supplier_ledger")
    source_id = models.UUIDField()
    kind = models.CharField(max_length=24)
    sequence = models.PositiveSmallIntegerField(default=0)
    external_reference = models.CharField(max_length=150, blank=True)
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField()
    delta_fcfa = models.BigIntegerField()
    debt_delta_fcfa = models.BigIntegerField(default=0)
    advance_delta_fcfa = models.BigIntegerField(default=0)
    credit_delta_fcfa = models.BigIntegerField(default=0)
    state = models.CharField(max_length=10, default="validated", choices=[
        ("validated", _("Validated")), ("pending", _("Pending")), ("rejected", _("Rejected"))])

    class Meta:
        db_table = "core_supplierledgerentry"
        ordering = ["occurred_at", "recorded_at", "operation_id", "sequence", "id"]
        indexes = [models.Index(fields=["shop", "supplier", "occurred_at", "id"], name="core_suppli_shop_id_e6fe49_idx")]
        constraints = [
            models.UniqueConstraint(fields=["operation", "sequence"], name="supplier_ledger_operation_sequence"),
            models.CheckConstraint(condition=models.Q(state__in=["validated", "pending", "rejected"]), name="supplier_ledger_known_state"),
            models.CheckConstraint(condition=_integer_columns("delta_fcfa", "debt_delta_fcfa", "advance_delta_fcfa", "credit_delta_fcfa", "sequence"), name="supplier_ledger_integer_effects"),
            models.CheckConstraint(condition=models.Func(models.F("delta_fcfa"), models.F("debt_delta_fcfa"), models.F("advance_delta_fcfa"), models.F("credit_delta_fcfa"), function="integer_balance_matches", output_field=models.BooleanField()), name="supplier_ledger_exact_balance"),
        ]


class PurchaseCharge(UUIDRecord):
    """A direct purchase cost is distinct from the invoice debt and any actual payment."""
    document = models.ForeignKey('purchasing.PurchaseDocument', on_delete=models.PROTECT, related_name="charges")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="purchase_charge")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="purchase_charges")
    occurred_at = models.DateTimeField()
    label = models.CharField(max_length=150)
    amount_fcfa = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_purchasecharge"
        ordering = ["occurred_at", "id"]
        constraints = [models.CheckConstraint(condition=_integer_columns("amount_fcfa"), name="purchase_charge_integer_amount")]


class PurchaseChargeRevision(UUIDRecord):
    """Append an allocation decision, preserving the previous manager's plan."""
    charge = models.ForeignKey('purchasing.PurchaseCharge', on_delete=models.PROTECT, related_name="revisions")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="purchase_charge_revision")
    version = models.PositiveBigIntegerField()
    supersedes = models.OneToOneField("self", on_delete=models.PROTECT, null=True, blank=True, related_name="superseded_by")

    class Meta:
        db_table = "core_purchasechargerevision"
        ordering = ["version", "id"]
        constraints = [
            models.UniqueConstraint(fields=["charge", "version"], name="purchase_charge_revision_once"),
            models.CheckConstraint(condition=models.Q(version__gt=0) & _integer_columns("version"), name="purchase_charge_revision_integer"),
            models.CheckConstraint(condition=(models.Q(version=1, supersedes__isnull=True) |
                models.Q(version__gt=1, supersedes__isnull=False)) & ~models.Q(supersedes=models.F("id")), name="purchase_charge_revision_origin"),
        ]


class PurchaseChargeAllocation(UUIDRecord):
    """Allocate a cost decision to a protected historical invoice line."""
    revision = models.ForeignKey('purchasing.PurchaseChargeRevision', on_delete=models.PROTECT, related_name="allocations")
    line = models.ForeignKey('purchasing.PurchaseLine', on_delete=models.PROTECT, related_name="charge_allocations")
    amount_fcfa = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_purchasechargeallocation"
        ordering = ["line__position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["revision", "line"], name="purchase_charge_line_once"),
            models.CheckConstraint(condition=_integer_columns("amount_fcfa"), name="purchase_charge_allocation_integer"),
        ]
