from decimal import Decimal

from django.db import models
from django.db.models.lookups import Exact, LessThan
from django.db.models.functions import Cast
from django.utils.translation import gettext_lazy as _
from common.model_base import UUIDRecord, _integer_columns
from catalogue.models import Product


class StockReservation(UUIDRecord):
    """An unresolved departure stays reserved after rejection; validation replaces it."""
    product = models.ForeignKey('catalogue.Product', on_delete=models.PROTECT, related_name="stock_reservations")
    operation = models.ForeignKey('core.Activity', on_delete=models.PROTECT, related_name="stock_reservations")
    quantity_milli = models.PositiveBigIntegerField()
    state = models.CharField(max_length=10, default="pending", choices=[
        ("pending", _("Pending")), ("validated", _("Validated")), ("rejected", _("Rejected")),
    ])

    class Meta:
        db_table = "core_stockreservation"
        indexes = [models.Index(fields=["product", "state"], name="core_stockr_product_509702_idx")]
        constraints = [
            models.UniqueConstraint(fields=["operation", "product"], name="stock_reservation_once"),
            models.CheckConstraint(condition=models.Q(quantity_milli__gt=0) & _integer_columns("quantity_milli"),
                                   name="stock_reservation_positive_integer"),
            models.CheckConstraint(condition=models.Q(state__in=["pending", "validated", "rejected"]),
                                   name="stock_reservation_known_state"),
        ]


class StockNotificationPreference(UUIDRecord):
    """One personal notification choice for each shop, shared across installations."""
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="stock_notification_preferences")
    user = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="stock_notification_preferences")
    notifications_enabled = models.BooleanField(default=True)

    class Meta:
        db_table = "core_stocknotificationpreference"
        constraints = [models.UniqueConstraint(fields=["shop", "user"], name="stock_notification_user_shop_once")]


class StockAlertState(UUIDRecord):
    """Current known threshold episode; rebuilt only by completed stock operations."""
    product = models.OneToOneField('catalogue.Product', on_delete=models.PROTECT, related_name="stock_alert_state")
    is_low = models.BooleanField(default=False)
    episode = models.PositiveBigIntegerField(default=0)

    class Meta:
        db_table = "core_stockalertstate"
        constraints = [models.CheckConstraint(condition=_integer_columns("episode"),
                                             name="stock_alert_state_integer_episode")]


class StockAlertEvent(UUIDRecord):
    """Durable crossing evidence, independent of delivery or user preferences."""
    product = models.ForeignKey('catalogue.Product', on_delete=models.PROTECT, related_name="stock_alert_events")
    operation = models.ForeignKey('core.Activity', on_delete=models.PROTECT, related_name="stock_alert_events")
    episode = models.PositiveBigIntegerField()
    available_milli = models.JSONField()
    threshold_milli = models.PositiveBigIntegerField()

    class Meta:
        db_table = "core_stockalertevent"
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["product", "created_at", "id"], name="core_stocka_product_6bf1db_idx")]
        constraints = [
            models.UniqueConstraint(fields=["product", "episode"], name="stock_alert_episode_once"),
            models.UniqueConstraint(fields=["product", "operation"], name="stock_alert_operation_once"),
            models.CheckConstraint(condition=models.Q(episode__gt=0) & _integer_columns("episode", "threshold_milli"),
                                   name="stock_alert_integer_episode_threshold"),
            models.CheckConstraint(condition=Exact(models.Func(models.F("available_milli"), function="JSON_TYPE", output_field=models.CharField()), models.Value("integer")),
                                   name="stock_alert_available_integer"),
        ]


class StockMovement(UUIDRecord):
    """Quantities in thousandths; an unknown opening cost remains explicitly null."""

    product = models.ForeignKey('catalogue.Product', on_delete=models.PROTECT, related_name="stock_movements")
    actor = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="stock_movements")
    operation = models.ForeignKey('core.Activity', on_delete=models.PROTECT, related_name="stock_movements")
    sale_line = models.OneToOneField('sales.SaleLine', on_delete=models.PROTECT, related_name="stock_movement", null=True, blank=True)
    kind = models.CharField(max_length=20, choices=[("opening", _("Opening stock")), ("sale", _("Sale")), ("purchase_receipt", _("Purchase receipt")), ("supplier_return", _("Supplier return")), ("loss", _("Loss")), ("inventory_adjustment", _("Inventory adjustment")), ("customer_return", _("Customer return")), ("unsellable_restock", _("Restock after inspection")), ("transfer_out", _("Transfer departure")), ("transfer_in", _("Transfer receipt"))])
    quantity_milli = models.BigIntegerField()
    unit_cost_micros = models.PositiveBigIntegerField(null=True, blank=True)
    occurred_at = models.DateTimeField()
    reason = models.TextField()

    @property
    def unit_cost_fcfa(self):
        if self.unit_cost_micros is None:
            return None
        return Decimal(self.unit_cost_micros) / Decimal(1000000)

    class Meta:
        db_table = "core_stockmovement"
        indexes = [models.Index(fields=["product", "occurred_at", "id"], name="core_stockm_product_18d1fd_idx")]
        constraints = [
            models.UniqueConstraint(fields=["operation"], condition=models.Q(kind="opening"), name="opening_operation_once"),
            models.CheckConstraint(condition=_integer_columns("quantity_milli"), name="stock_movement_integer_quantity"),
            models.CheckConstraint(condition=models.Q(unit_cost_micros__isnull=True) | _integer_columns("unit_cost_micros"),
                                   name="stock_movement_integer_known_cost"),
        ]


class StockLoss(UUIDRecord):
    """A durable observation of physical loss; it is neither a sale nor a cost claim."""
    product = models.ForeignKey('catalogue.Product', on_delete=models.PROTECT, related_name="losses")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="stock_loss")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT)
    movement = models.OneToOneField('stock.StockMovement', on_delete=models.PROTECT, related_name="loss")
    quantity_milli = models.PositiveBigIntegerField()
    unit_kind = models.CharField(max_length=10, choices=Product.UnitKind.choices)
    unit_label = models.CharField(max_length=40)
    occurred_at = models.DateTimeField()
    stock_after_milli = models.JSONField()
    stock_requires_regularization = models.BooleanField(default=False)

    class Meta:
        db_table = "core_stockloss"
        ordering = ["occurred_at", "id"]
        indexes = [models.Index(fields=["product", "occurred_at", "id"], name="core_stockl_product_85e209_idx")]
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity_milli__gt=0) & _integer_columns("quantity_milli"), name="stock_loss_positive_integer"),
            models.CheckConstraint(condition=Exact(models.Func(models.F("stock_after_milli"), function="JSON_TYPE", output_field=models.CharField()), models.Value("integer")), name="stock_loss_after_integer"),
            models.CheckConstraint(condition=(
                (models.Q(unit_kind="piece") & Exact(models.F("quantity_milli") % 1000, models.Value(0)))
                | models.Q(unit_kind__in=["weight", "volume"])
            ), name="stock_loss_unit_precision"),
            # Cast is used only for the sign; even int64 saturation preserves it.
            models.CheckConstraint(condition=Exact(
                models.F("stock_requires_regularization"),
                LessThan(Cast(models.F("stock_after_milli"), output_field=models.BigIntegerField()), models.Value(0)),
            ), name="stock_loss_negative_flag"),
        ]


class StockCount(UUIDRecord):
    """A fixed physical observation, independent of subsequent stock corrections."""
    product = models.ForeignKey('catalogue.Product', on_delete=models.PROTECT, related_name="stock_counts")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="stock_count")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT)
    physical_milli = models.PositiveBigIntegerField()
    unit_kind = models.CharField(max_length=10, choices=Product.UnitKind.choices)
    unit_label = models.CharField(max_length=40)
    counted_at = models.DateTimeField()
    expected_at_creation_milli = models.JSONField()

    class Meta:
        db_table = "core_stockcount"
        ordering = ["counted_at", "id"]
        indexes = [models.Index(fields=["product", "counted_at", "id"], name="core_stockc_product_4db4e7_idx")]
        constraints = [
            models.CheckConstraint(condition=_integer_columns("physical_milli"), name="stock_count_physical_integer"),
            models.CheckConstraint(condition=Exact(models.Func(models.F("expected_at_creation_milli"), function="JSON_TYPE", output_field=models.CharField()), models.Value("integer")), name="stock_count_expected_integer"),
            models.CheckConstraint(condition=((models.Q(unit_kind="piece") & Exact(models.F("physical_milli") % 1000, models.Value(0)))
                                               | models.Q(unit_kind__in=["weight", "volume"])), name="stock_count_unit_precision"),
        ]


class StockCountAdjustment(UUIDRecord):
    """An explicit adjustment preserves the previous count and every earlier decision."""
    count = models.ForeignKey('stock.StockCount', on_delete=models.PROTECT, related_name="adjustments")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="stock_count_adjustment")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT)
    movement = models.OneToOneField('stock.StockMovement', on_delete=models.PROTECT, related_name="count_adjustment", null=True, blank=True)
    expected_milli = models.JSONField()
    target_adjustment_milli = models.JSONField()
    delta_milli = models.BigIntegerField()

    class Meta:
        db_table = "core_stockcountadjustment"
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=_integer_columns("delta_milli"), name="count_adjustment_delta_integer"),
            models.CheckConstraint(condition=Exact(models.Func(models.F("expected_milli"), function="JSON_TYPE", output_field=models.CharField()), models.Value("integer")), name="count_adjustment_expected_int"),
            models.CheckConstraint(condition=Exact(models.Func(models.F("target_adjustment_milli"), function="JSON_TYPE", output_field=models.CharField()), models.Value("integer")), name="count_adjustment_target_int"),
            models.CheckConstraint(condition=(models.Q(delta_milli=0, movement__isnull=True) | (~models.Q(delta_milli=0) & models.Q(movement__isnull=False))), name="count_adjustment_movement_exact"),
        ]


class ReturnReceipt(UUIDRecord):
    """Physical goods received are distinct from the customer's financial return."""
    return_line = models.ForeignKey('sales.SaleReturnLine', on_delete=models.PROTECT, related_name="receipts")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="return_receipt")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="return_receipts")
    quantity_milli = models.PositiveBigIntegerField()
    condition = models.CharField(max_length=12, choices=[("sellable", _("Sellable")), ("unsellable", _("Unsellable"))])
    movement = models.OneToOneField('stock.StockMovement', on_delete=models.PROTECT, null=True, blank=True, related_name="return_receipt")
    unit_cost_micros = models.PositiveBigIntegerField(null=True, blank=True)
    cost_is_provisional = models.BooleanField(default=False)
    occurred_at = models.DateTimeField()

    class Meta:
        db_table = "core_returnreceipt"
        ordering = ["occurred_at", "id"]
        indexes = [models.Index(fields=["return_line", "occurred_at", "id"], name="core_return_return__ad70df_idx")]
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity_milli__gt=0) & _integer_columns("quantity_milli"), name="return_receipt_positive_integer"),
            models.CheckConstraint(condition=models.Q(unit_cost_micros__isnull=True) | _integer_columns("unit_cost_micros"), name="return_receipt_exact_cost"),
            models.CheckConstraint(condition=(models.Q(condition="sellable", movement__isnull=False) |
                models.Q(condition="unsellable", movement__isnull=True)), name="return_receipt_available_effect"),
        ]


class NonSellableDecision(UUIDRecord):
    """Dispose of a physical return once without deducting available stock twice."""
    receipt = models.ForeignKey('stock.ReturnReceipt', on_delete=models.PROTECT, related_name="decisions")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="non_sellable_decision")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="non_sellable_decisions")
    class Kind(models.TextChoices):
        RESTOCK = "restock", _("Restock after inspection")
        SUPPLIER_RETURN = "supplier_return", _("Return to supplier")
        SCRAP = "scrap", _("Scrap")

    kind = models.CharField(max_length=20, choices=Kind.choices)
    quantity_milli = models.PositiveBigIntegerField()
    after_quantity_milli = models.PositiveBigIntegerField()
    unit_kind = models.CharField(max_length=10)
    is_sellable_confirmed = models.BooleanField(default=False)
    movement = models.OneToOneField('stock.StockMovement', on_delete=models.PROTECT, null=True, blank=True, related_name="non_sellable_decision")
    supplier = models.ForeignKey('purchasing.Supplier', on_delete=models.PROTECT, null=True, blank=True, related_name="non_sellable_decisions")
    occurred_at = models.DateTimeField()

    class Meta:
        db_table = "core_nonsellabledecision"
        ordering = ["occurred_at", "id"]
        indexes = [models.Index(fields=["receipt", "occurred_at", "id"], name="core_nonsel_receipt_fe57a8_idx")]
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity_milli__gt=0) & _integer_columns("quantity_milli", "after_quantity_milli"),
                name="unsellable_decision_integer_qty"),
            models.CheckConstraint(condition=models.Q(unit_kind__in=["piece", "weight", "volume"]) & (~models.Q(unit_kind="piece") |
                (Exact(models.F("quantity_milli") % 1000, models.Value(0)) & Exact(models.F("after_quantity_milli") % 1000, models.Value(0)))),
                name="unsellable_decision_precision"),
            models.CheckConstraint(condition=(models.Q(kind="restock", movement__isnull=False, supplier__isnull=True, is_sellable_confirmed=True)
                | models.Q(kind="supplier_return", movement__isnull=True, supplier__isnull=False, is_sellable_confirmed=False)
                | models.Q(kind="scrap", movement__isnull=True, supplier__isnull=True, is_sellable_confirmed=False)),
                name="unsellable_decision_effect"),
        ]


class StockShortageReview(UUIDRecord):
    """Keep the manager's physical-shortage decision separate from cost validation."""
    product = models.ForeignKey('catalogue.Product', on_delete=models.PROTECT, related_name="shortage_reviews")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="stock_shortage_review")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="stock_shortage_reviews")
    count = models.ForeignKey('stock.StockCount', on_delete=models.PROTECT, null=True, blank=True, related_name="shortage_reviews")
    occurred_at = models.DateTimeField()
    stock_quantity_milli = models.JSONField()
    stock_value_nanos_fcfa = models.JSONField(null=True, blank=True)
    basis_fingerprint = models.CharField(max_length=64)

    class Meta:
        db_table = "core_stockshortagereview"
        indexes = [models.Index(fields=["product", "occurred_at", "id"], name="stock_shortage_product_time")]
        constraints = [models.CheckConstraint(condition=models.Func(models.F("stock_quantity_milli"),
            models.F("stock_value_nanos_fcfa"), function="stock_shortage_review_valid", output_field=models.BooleanField()),
            name="stock_shortage_review_values")]

    @property
    def kind(self):
        return "shortage_review"

    @property
    def quantity_milli(self):
        return 0

    @property
    def sale_line_id(self):
        return None


class StockValuationAdjustment(UUIDRecord):
    """Confirm only the value of remaining stock, keeping historical operations intact."""
    product = models.ForeignKey('catalogue.Product', on_delete=models.PROTECT, related_name="valuation_adjustments")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="stock_valuation_adjustment")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="stock_valuation_adjustments")
    occurred_at = models.DateTimeField()
    stock_quantity_milli = models.JSONField()
    previous_value_nanos_fcfa = models.JSONField(null=True, blank=True)
    value_fcfa = models.JSONField()
    basis_fingerprint = models.CharField(max_length=64)

    class Meta:
        db_table = "core_stockvaluationadjustment"
        indexes = [models.Index(fields=["product", "occurred_at", "id"], name="stock_valuation_product_time")]
        constraints = [models.CheckConstraint(condition=models.Func(models.F("stock_quantity_milli"),
            models.F("previous_value_nanos_fcfa"), models.F("value_fcfa"), function="stock_valuation_closure_valid",
            output_field=models.BooleanField()), name="stock_valuation_or_closure")]

    @property
    def kind(self):
        return "valuation_adjustment"

    @property
    def quantity_milli(self):
        return 0

    @property
    def sale_line_id(self):
        return None


class StockCostSnapshot(UUIDRecord):
    """Append cost versions without replacing the first valuation or commercial facts."""
    movement = models.ForeignKey('stock.StockMovement', on_delete=models.PROTECT, related_name="cost_snapshots")
    trigger_operation = models.ForeignKey('core.Activity', on_delete=models.PROTECT, related_name="cost_snapshots")
    version = models.PositiveBigIntegerField()
    supersedes = models.OneToOneField("self", on_delete=models.PROTECT, null=True, blank=True, related_name="superseded_by")
    cost_state = models.CharField(max_length=12, choices=[("known", _("Known")), ("provisional", _("Provisional")), ("unknown", _("Unknown"))])
    total_cost_nanos_fcfa = models.JSONField(null=True, blank=True)
    unit_cost_micros_numerator = models.JSONField(null=True, blank=True)
    unit_cost_micros_denominator = models.JSONField(null=True, blank=True)
    quantity_after_milli = models.JSONField()
    stock_value_after_nanos_fcfa = models.JSONField(null=True, blank=True)
    valuation_requires_regularization = models.BooleanField(null=True, default=None)
    valuation_gap_nanos_fcfa = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "core_stockcostsnapshot"
        ordering = ["movement_id", "version"]
        constraints = [
            models.CheckConstraint(condition=models.Func(models.F("valuation_requires_regularization"),
                models.F("valuation_gap_nanos_fcfa"), function="cost_valuation_gap_valid", output_field=models.BooleanField()),
                name="cost_snapshot_valuation_gap"),
            models.UniqueConstraint(fields=["movement", "version"], name="cost_snapshot_movement_version"),
            models.UniqueConstraint(fields=["movement", "trigger_operation"], name="cost_snapshot_trigger_once"),
            models.CheckConstraint(condition=models.Q(version__gt=0) & _integer_columns("version"), name="cost_snapshot_positive_version"),
            models.CheckConstraint(condition=(models.Q(version=1, supersedes__isnull=True) |
                models.Q(version__gt=1, supersedes__isnull=False)) & ~models.Q(supersedes=models.F("id")), name="cost_snapshot_revision_origin"),
            models.CheckConstraint(condition=models.Func(models.F("cost_state"), models.F("total_cost_nanos_fcfa"),
                models.F("unit_cost_micros_numerator"), models.F("unit_cost_micros_denominator"), models.F("quantity_after_milli"),
                models.F("stock_value_after_nanos_fcfa"), function="cost_snapshot_values_valid", output_field=models.BooleanField()),
                name="cost_snapshot_exact_state"),
        ]


class ReturnCostSnapshot(UUIDRecord):
    """Freeze a physical receipt's share of its original sale cost, including uncertainty."""
    receipt = models.OneToOneField('stock.ReturnReceipt', on_delete=models.PROTECT, related_name="cost_snapshot")
    sale_cost = models.ForeignKey('stock.StockCostSnapshot', on_delete=models.PROTECT, related_name="physical_returns", null=True, blank=True)
    cost_state = models.CharField(max_length=12, choices=[("known", _("Known")), ("provisional", _("Provisional")), ("unknown", _("Unknown"))])
    total_cost_nanos_fcfa = models.JSONField(null=True, blank=True)
    unit_cost_micros_numerator = models.JSONField(null=True, blank=True)
    unit_cost_micros_denominator = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "core_returncostsnapshot"
        constraints = [models.CheckConstraint(condition=models.Func(models.F("cost_state"), models.F("total_cost_nanos_fcfa"),
            models.F("unit_cost_micros_numerator"), models.F("unit_cost_micros_denominator"), models.Value("0"), models.Value("0"),
            function="cost_snapshot_values_valid", output_field=models.BooleanField()), name="return_cost_exact_state")]


class CostRevaluation(UUIDRecord):
    """One audited cost replay, separate from physical and financial operations."""
    product = models.ForeignKey('catalogue.Product', on_delete=models.PROTECT, related_name="cost_revaluations")
    operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, related_name="cost_revaluation")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="cost_revaluations")
    source_revision_ids = models.JSONField(default=list)

    class Meta:
        db_table = "core_costrevaluation"
        indexes = [models.Index(fields=["product", "created_at", "id"], name="core_costre_product_97e46b_idx")]


class CostRevaluationItem(UUIDRecord):
    batch = models.ForeignKey('stock.CostRevaluation', on_delete=models.PROTECT, related_name="items")
    movement = models.ForeignKey('stock.StockMovement', on_delete=models.PROTECT, related_name="cost_revaluation_items")
    previous = models.ForeignKey('stock.StockCostSnapshot', on_delete=models.PROTECT, null=True, blank=True, related_name="revaluation_before")
    revised = models.ForeignKey('stock.StockCostSnapshot', on_delete=models.PROTECT, related_name="revaluation_after")
    previous_legacy_unit_cost_micros = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "core_costrevaluationitem"
        constraints = [
            models.UniqueConstraint(fields=["batch", "movement"], name="revaluation_movement_once"),
            models.CheckConstraint(condition=~models.Q(previous=models.F("revised")), name="revaluation_distinct_costs"),
        ]


class ReturnCostRevision(UUIDRecord):
    """Preserve the physical receipt's original cost while tracing later valuations."""
    origin = models.ForeignKey('stock.ReturnCostSnapshot', on_delete=models.PROTECT, related_name="revisions")
    batch = models.ForeignKey('stock.CostRevaluation', on_delete=models.PROTECT, related_name="return_revisions")
    version = models.PositiveBigIntegerField()
    supersedes = models.OneToOneField("self", on_delete=models.PROTECT, null=True, blank=True, related_name="superseded_by")
    sale_cost = models.ForeignKey('stock.StockCostSnapshot', on_delete=models.PROTECT, null=True, blank=True, related_name="revised_physical_returns")
    cost_state = models.CharField(max_length=12, choices=ReturnCostSnapshot._meta.get_field("cost_state").choices)
    total_cost_nanos_fcfa = models.JSONField(null=True, blank=True)
    unit_cost_micros_numerator = models.JSONField(null=True, blank=True)
    unit_cost_micros_denominator = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "core_returncostrevision"
        ordering = ["version", "id"]
        indexes = [models.Index(fields=["batch", "created_at", "id"], name="core_return_batch_i_a42321_idx")]
        constraints = [
            models.UniqueConstraint(fields=["origin", "version"], name="return_cost_revision_version"),
            models.UniqueConstraint(fields=["origin", "batch"], name="return_cost_revision_batch_once"),
            models.CheckConstraint(condition=_integer_columns("version") & (models.Q(version=2, supersedes__isnull=True) |
                models.Q(version__gt=2, supersedes__isnull=False)) & ~models.Q(supersedes=models.F("id")), name="return_cost_revision_origin"),
            models.CheckConstraint(condition=models.Func(models.F("cost_state"), models.F("total_cost_nanos_fcfa"),
                models.F("unit_cost_micros_numerator"), models.F("unit_cost_micros_denominator"), models.Value("0"), models.Value("0"),
                function="cost_snapshot_values_valid", output_field=models.BooleanField()), name="return_cost_revision_exact_state"),
        ]


class ReturnCostReconstruction(UUIDRecord):
    """Identify a legacy baseline captured during an explicit audited replay."""
    original = models.OneToOneField('stock.ReturnCostSnapshot', on_delete=models.PROTECT, related_name="reconstruction")
    batch = models.ForeignKey('stock.CostRevaluation', on_delete=models.PROTECT, related_name="return_reconstructions")
    previous_legacy_unit_cost_micros = models.PositiveBigIntegerField(null=True, blank=True)
    previous_cost_is_provisional = models.BooleanField()

    class Meta:
        db_table = "core_returncostreconstruction"
        constraints = [models.CheckConstraint(condition=models.Q(previous_legacy_unit_cost_micros__isnull=True) |
            _integer_columns("previous_legacy_unit_cost_micros"), name="return_reconstruction_integer")]


from stock.reconciliation_models import StockReservationReconciliation  # noqa: E402,F401
from stock.transfer_models import StockTransfer, StockTransferLine, StockTransferReceipt, StockTransferReceiptLine  # noqa: E402,F401
