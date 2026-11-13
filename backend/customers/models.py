import uuid

from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _
from common.model_base import UUIDRecord, _integer_columns
from sales.models import SalePayment


class Customer(UUIDRecord):
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="customers")
    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=16, null=True, blank=True, validators=[RegexValidator(r"^\+[1-9][0-9]{7,14}$")])
    version = models.PositiveBigIntegerField(default=1)
    # G20: optional reference from a template CSV, unique per shop for this type.
    import_reference = models.CharField(max_length=80, null=True, blank=True)
    # G18: a merged record keeps its identity; operations received later follow this redirection.
    merged_into = models.ForeignKey('customers.Customer', on_delete=models.PROTECT, null=True, blank=True, related_name="merged_sources")

    class Meta:
        db_table = "core_customer"
        ordering = ["created_at", "id"]
        constraints = [models.UniqueConstraint(fields=["shop", "import_reference"], condition=models.Q(import_reference__isnull=False),
                                               name="customer_import_reference_once"),
                       models.CheckConstraint(condition=models.Q(version__gt=0) & _integer_columns("version"),
                                             name="customer_positive_integer_version")]
        indexes = [
            models.Index(fields=["shop", "created_at", "id"], name="core_custom_shop_id_71994d_idx"),
            models.Index(fields=["shop", "name"], name="customer_shop_name_match"),
            models.Index(fields=["shop", "phone"], name="customer_shop_phone_match"),
        ]


class CustomerDebt(UUIDRecord):
    """An outstanding credit portion; money received is stored in separate events."""
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="customer_debts")
    customer = models.ForeignKey('customers.Customer', on_delete=models.PROTECT, related_name="debts")
    # G18: the merged record that received this debt when it was redirected to `customer`.
    received_for = models.ForeignKey('customers.Customer', on_delete=models.PROTECT, null=True, blank=True, related_name="redirected_debts")
    sale = models.OneToOneField('sales.Sale', on_delete=models.PROTECT, related_name="debt", null=True, blank=True)
    actor = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="customer_debts")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="customer_debts")
    operation = models.ForeignKey('core.Activity', on_delete=models.PROTECT, related_name="customer_debts")
    amount_fcfa = models.PositiveBigIntegerField()
    remaining_fcfa = models.PositiveBigIntegerField()
    occurred_at = models.DateTimeField()
    due_at = models.DateTimeField(null=True, blank=True)
    due_version = models.PositiveBigIntegerField(default=1)

    class Meta:
        db_table = "core_customerdebt"
        ordering = ["occurred_at", "id"]
        indexes = [models.Index(fields=["shop", "customer", "occurred_at", "id"], name="core_custom_shop_id_6f4c80_idx")]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount_fcfa__gt=0), name="customer_debt_positive_amount"),
            models.CheckConstraint(condition=models.Q(remaining_fcfa__lte=models.F("amount_fcfa")), name="customer_debt_remaining_bound"),
            models.CheckConstraint(condition=models.Q(due_version__gt=0), name="customer_debt_due_version_positive"),
            models.CheckConstraint(condition=_integer_columns("amount_fcfa", "remaining_fcfa", "due_version"), name="customer_debt_integral_values"),
        ]


class CustomerPayment(UUIDRecord):
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="customer_payments")
    customer = models.ForeignKey('customers.Customer', on_delete=models.PROTECT, related_name="payments")
    actor = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="customer_payments")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="customer_payments")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="customer_payment")
    occurred_at = models.DateTimeField()
    amount_fcfa = models.PositiveBigIntegerField()
    unallocated_fcfa = models.PositiveBigIntegerField()
    requires_regularization = models.BooleanField(default=False)

    class Meta:
        db_table = "core_customerpayment"
        ordering = ["occurred_at", "id"]
        indexes = [models.Index(fields=["shop", "customer", "occurred_at", "id"], name="core_custom_shop_id_545c88_idx")]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount_fcfa__gt=0), name="customer_payment_positive_amount"),
            models.CheckConstraint(condition=models.Q(unallocated_fcfa__lte=models.F("amount_fcfa")), name="customer_payment_unallocated_bound"),
            models.CheckConstraint(condition=_integer_columns("amount_fcfa", "unallocated_fcfa"), name="customer_payment_integral_values"),
        ]


class CustomerPaymentPart(UUIDRecord):
    payment = models.ForeignKey('customers.CustomerPayment', on_delete=models.PROTECT, related_name="parts")
    position = models.PositiveIntegerField()
    method = models.CharField(max_length=10, choices=SalePayment.Method.choices)
    other_name = models.CharField(max_length=80, blank=True)
    amount_fcfa = models.PositiveBigIntegerField()
    external_reference = models.CharField(max_length=150, blank=True)
    received_fcfa = models.PositiveBigIntegerField(null=True, blank=True)
    returned_fcfa = models.PositiveBigIntegerField(null=True, blank=True)
    possible_duplicate_reference = models.BooleanField(default=False)

    class Meta:
        db_table = "core_customerpaymentpart"
        ordering = ["position", "id"]
        indexes = [models.Index(fields=["external_reference", "method"], name="core_custom_externa_c2f99d_idx")]
        constraints = [
            models.UniqueConstraint(fields=["payment", "position"], name="customer_payment_part_position"),
            models.CheckConstraint(condition=models.Q(amount_fcfa__gt=0), name="customer_payment_part_positive"),
            models.CheckConstraint(condition=_integer_columns("position", "amount_fcfa")
                & (models.Q(received_fcfa__isnull=True) | _integer_columns("received_fcfa"))
                & (models.Q(returned_fcfa__isnull=True) | _integer_columns("returned_fcfa")), name="customer_payment_part_integers"),
            models.CheckConstraint(condition=(
                models.Q(received_fcfa__isnull=True, returned_fcfa__isnull=True)
                | (models.Q(received_fcfa__isnull=False, returned_fcfa__isnull=False)
                   & models.Q(received_fcfa=models.F("amount_fcfa") + models.F("returned_fcfa")))
            ), name="customer_payment_change_exact"),
        ]


class DebtAllocation(UUIDRecord):
    payment = models.ForeignKey('customers.CustomerPayment', on_delete=models.PROTECT, related_name="allocations")
    debt = models.ForeignKey('customers.CustomerDebt', on_delete=models.PROTECT, related_name="allocations")
    position = models.PositiveIntegerField()
    requested_amount_fcfa = models.PositiveBigIntegerField()
    applied_amount_fcfa = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_debtallocation"
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["payment", "position"], name="debt_allocation_position"),
            models.CheckConstraint(condition=models.Q(requested_amount_fcfa__gt=0), name="debt_allocation_positive_request"),
            models.CheckConstraint(condition=models.Q(applied_amount_fcfa__lte=models.F("requested_amount_fcfa")), name="debt_allocation_applied_bound"),
            models.CheckConstraint(condition=_integer_columns("position", "requested_amount_fcfa", "applied_amount_fcfa"), name="debt_allocation_integral_values"),
        ]


class PaymentRegularization(UUIDRecord):
    payment = models.ForeignKey('customers.CustomerPayment', on_delete=models.PROTECT, related_name="regularizations", null=True, blank=True)
    credit = models.ForeignKey('customers.CustomerCredit', on_delete=models.PROTECT, related_name="regularizations", null=True, blank=True)
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="payment_regularization")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="payment_regularizations")
    kind = models.CharField(max_length=12, choices=[("allocation", _("Allocation")), ("refund", _("Refund"))])
    amount_fcfa = models.PositiveBigIntegerField()
    after_unallocated_fcfa = models.PositiveBigIntegerField()
    occurred_at = models.DateTimeField()
    method = models.CharField(max_length=10, choices=SalePayment.Method.choices, blank=True)
    other_name = models.CharField(max_length=80, blank=True)
    external_reference = models.CharField(max_length=150, blank=True)

    class Meta:
        db_table = "core_paymentregularization"
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=_integer_columns("amount_fcfa", "after_unallocated_fcfa"),
                                   name="regularization_integral_amounts"),
            models.CheckConstraint(condition=models.Q(amount_fcfa__gt=0), name="payment_regularization_positive"),
            models.CheckConstraint(condition=(models.Q(payment__isnull=False, credit__isnull=True)
                                               | models.Q(payment__isnull=True, credit__isnull=False)),
                                   name="regularization_one_money_source"),
        ]


class RegularizationAllocation(UUIDRecord):
    regularization = models.ForeignKey('customers.PaymentRegularization', on_delete=models.PROTECT, related_name="allocations")
    debt = models.ForeignKey('customers.CustomerDebt', on_delete=models.PROTECT, related_name="regularization_allocations")
    position = models.PositiveIntegerField()
    amount_fcfa = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_regularizationallocation"
        ordering = ["position", "id"]
        constraints = [
            models.CheckConstraint(condition=_integer_columns("position", "amount_fcfa"),
                                   name="regularization_alloc_integers"),
            models.UniqueConstraint(fields=["regularization", "position"], name="regularization_allocation_position"),
            models.CheckConstraint(condition=models.Q(amount_fcfa__gt=0), name="regularization_allocation_positive"),
        ]


class CustomerLedgerEntry(models.Model):
    """Immutable statement effects, recorded with their source operation's clock."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="customer_ledger")
    customer = models.ForeignKey('customers.Customer', on_delete=models.PROTECT, related_name="ledger_entries")
    operation = models.ForeignKey('core.Activity', on_delete=models.PROTECT, related_name="customer_ledger")
    source_id = models.UUIDField()
    kind = models.CharField(max_length=24)
    sequence = models.PositiveSmallIntegerField(default=0)
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField()
    delta_fcfa = models.BigIntegerField()
    debt_delta_fcfa = models.BigIntegerField(default=0)
    credit_delta_fcfa = models.BigIntegerField(default=0)
    state = models.CharField(max_length=10, choices=[("validated", _("Validated")), ("pending", _("Pending")), ("rejected", _("Rejected"))], default="validated")

    class Meta:
        db_table = "core_customerledgerentry"
        ordering = ["occurred_at", "recorded_at", "operation_id", "sequence", "id"]
        indexes = [models.Index(fields=["shop", "customer", "occurred_at", "id"], name="core_custom_shop_id_c21381_idx")]
        constraints = [models.UniqueConstraint(fields=["operation", "sequence"], name="customer_ledger_operation_sequence"),
            models.CheckConstraint(condition=_integer_columns("delta_fcfa", "debt_delta_fcfa", "credit_delta_fcfa", "sequence"),
                                   name="customer_ledger_integral_values"),
            models.CheckConstraint(condition=models.Q(state__in=["validated", "pending", "rejected"]),
                                   name="customer_ledger_known_state")]


class CustomerCredit(UUIDRecord):
    """Money owed to a client; it does not automatically offset other receivables."""
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="customer_credits")
    customer = models.ForeignKey('customers.Customer', on_delete=models.PROTECT, related_name="credits")
    sale = models.OneToOneField('sales.Sale', on_delete=models.PROTECT, related_name="customer_credit", null=True, blank=True)
    operation = models.ForeignKey('core.Activity', on_delete=models.PROTECT, related_name="customer_credits")
    amount_fcfa = models.PositiveBigIntegerField()
    remaining_fcfa = models.PositiveBigIntegerField()
    occurred_at = models.DateTimeField()
    kind = models.CharField(max_length=24, default="sale_excess")

    class Meta:
        db_table = "core_customercredit"
        ordering = ["occurred_at", "id"]
        indexes = [models.Index(fields=["shop", "customer", "occurred_at", "id"], name="core_custom_shop_id_c03b07_idx")]
        constraints = [
            models.CheckConstraint(condition=_integer_columns("amount_fcfa", "remaining_fcfa"),
                                   name="customer_credit_integral_amounts"),
            models.CheckConstraint(condition=models.Q(amount_fcfa__gt=0), name="customer_credit_positive_amount"),
            models.CheckConstraint(condition=models.Q(remaining_fcfa__lte=models.F("amount_fcfa")), name="customer_credit_remaining_bound"),
        ]


class ReturnCreditSettlement(UUIDRecord):
    """An explicit use of return credit, distinct from the return and its receipt."""
    sale_return = models.ForeignKey('sales.SaleReturn', on_delete=models.PROTECT, related_name="settlements")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="return_credit_settlement")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="return_credit_settlements")
    kind = models.CharField(max_length=12, choices=[("refund", _("Refund")), ("allocation", _("Allocation"))])
    amount_fcfa = models.PositiveBigIntegerField()
    after_credit_fcfa = models.PositiveBigIntegerField()
    method = models.CharField(max_length=10, choices=SalePayment.Method.choices, blank=True)
    other_name = models.CharField(max_length=80, blank=True)
    external_reference = models.CharField(max_length=150, blank=True)
    occurred_at = models.DateTimeField()

    class Meta:
        db_table = "core_returncreditsettlement"
        ordering = ["occurred_at", "id"]
        indexes = [models.Index(fields=["sale_return", "occurred_at", "id"], name="core_return_sale_re_29bbe7_idx")]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount_fcfa__gt=0) & _integer_columns("amount_fcfa", "after_credit_fcfa"), name="return_credit_settlement_integers"),
            models.CheckConstraint(condition=(models.Q(kind="allocation", method="", other_name="", external_reference="")
                | (models.Q(kind="refund", method__in=["cash", "mtn", "orange", "bank", "other"])
                   & ((models.Q(method="other") & ~models.Q(other_name="")) | (~models.Q(method="other") & models.Q(other_name=""))))),
                name="return_credit_settlement_kind"),
        ]


class ReturnCreditAllocation(UUIDRecord):
    settlement = models.ForeignKey('customers.ReturnCreditSettlement', on_delete=models.PROTECT, related_name="allocations")
    debt = models.ForeignKey('customers.CustomerDebt', on_delete=models.PROTECT, related_name="return_credit_allocations")
    amount_fcfa = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_returncreditallocation"
        constraints = [
            models.UniqueConstraint(fields=["settlement", "debt"], name="return_credit_debt_once"),
            models.CheckConstraint(condition=models.Q(amount_fcfa__gt=0) & _integer_columns("amount_fcfa"), name="return_credit_allocation_integer"),
        ]


class CustomerReminder(UUIDRecord):
    """US-25 / Q83–Q88: a frozen WhatsApp reminder; "sent" is only ever declared by a person."""
    class Status(models.TextChoices):
        PREPARED = "prepared", _("Prepared")
        SENT_DECLARED = "sent_declared", _("Sent (declared)")

    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="customer_reminders")
    customer = models.ForeignKey('customers.Customer', on_delete=models.PROTECT, related_name="reminders")
    actor = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="customer_reminders")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="customer_reminders")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="customer_reminder")
    language = models.CharField(max_length=2, choices=[("fr", "Français"), ("en", "English")])
    phone = models.CharField(max_length=16, null=True, blank=True)
    message = models.TextField()
    total_fcfa = models.PositiveBigIntegerField()
    status = models.CharField(max_length=13, choices=Status.choices, default=Status.PREPARED)
    declared_at = models.DateTimeField(null=True, blank=True)
    declared_by = models.ForeignKey('core.User', on_delete=models.PROTECT, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["shop", "customer", "created_at", "id"], name="customer_reminder_list_idx")]
        constraints = [
            models.CheckConstraint(condition=models.Q(total_fcfa__gt=0) & _integer_columns("total_fcfa"),
                                   name="customer_reminder_positive_total"),
            models.CheckConstraint(condition=models.Q(status__in=["prepared", "sent_declared"]),
                                   name="customer_reminder_known_status"),
            models.CheckConstraint(
                condition=(models.Q(status="prepared", declared_at__isnull=True, declared_by__isnull=True)
                           | models.Q(status="sent_declared", declared_at__isnull=False, declared_by__isnull=False)),
                name="customer_reminder_declaration_consistent"),
        ]


class CustomerReminderDebt(models.Model):
    """The debt amounts as frozen in the prepared text."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reminder = models.ForeignKey('customers.CustomerReminder', on_delete=models.PROTECT, related_name="debts")
    debt = models.ForeignKey('customers.CustomerDebt', on_delete=models.PROTECT, related_name="+")
    remaining_fcfa = models.PositiveBigIntegerField()
    overdue = models.BooleanField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["reminder", "debt"], name="customer_reminder_debt_once"),
            models.CheckConstraint(condition=models.Q(remaining_fcfa__gt=0) & _integer_columns("remaining_fcfa"),
                                   name="customer_reminder_debt_positive"),
        ]
