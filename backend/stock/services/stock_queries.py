"""Exact SQLite stock aggregates, kept in SQL for filtering and pagination."""
from django.db.models import Aggregate, BooleanField, CharField, F, Func, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from stock.models import StockMovement, StockReservation, StockReservationReconciliation


class IntegerTotal:
    def __init__(self):
        self.total = 0

    def step(self, quantity):
        if quantity is not None:
            self.total += int(quantity)

    def finalize(self):
        # SQLite callbacks cannot return integers exceeding int64; decimal text is exact.
        return str(self.total)


def register_stock_functions(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        connection.connection.create_aggregate("stock_integer_total", 1, IntegerTotal)
        connection.connection.create_function("stock_integer_subtract", 2,
            lambda left, right: str(int(left) - int(right)), deterministic=True)
        connection.connection.create_function("stock_at_threshold", 2,
            lambda available, threshold: threshold is not None and int(available) <= int(threshold), deterministic=True)


class StockTotal(Aggregate):
    function = "stock_integer_total"
    output_field = CharField()


def with_stock(queryset):
    movements = StockMovement.objects.filter(product_id=OuterRef("pk")).order_by().values("product_id").annotate(total=StockTotal("quantity_milli")).values("total")
    reservations = StockReservation.objects.filter(product_id=OuterRef("pk"), state__in=["pending", "rejected"])
    rejected = reservations.filter(state="rejected").order_by().values("product_id").annotate(total=StockTotal("quantity_milli")).values("total")
    reservations = reservations.order_by().values("product_id").annotate(total=StockTotal("quantity_milli")).values("total")
    reconciled = StockReservationReconciliation.objects.filter(reservation__product_id=OuterRef("pk"),
        reservation__state="rejected").order_by().values("reservation__product_id").annotate(total=StockTotal("quantity_milli")).values("total")
    return queryset.annotate(
        stock_validated_milli=Coalesce(Subquery(movements), Value("0"), output_field=CharField()),
        stock_reserved_original_milli=Coalesce(Subquery(reservations), Value("0"), output_field=CharField()),
        stock_rejected_original_milli=Coalesce(Subquery(rejected), Value("0"), output_field=CharField()),
        stock_reconciled_reserved_milli=Coalesce(Subquery(reconciled), Value("0"), output_field=CharField()),
    ).annotate(stock_reserved_milli=Func(F("stock_reserved_original_milli"), F("stock_reconciled_reserved_milli"),
                                      function="stock_integer_subtract", output_field=CharField()),
               stock_rejected_reserved_milli=Func(F("stock_rejected_original_milli"), F("stock_reconciled_reserved_milli"),
                                      function="stock_integer_subtract", output_field=CharField())
    ).annotate(stock_pending_reserved_milli=Func(F("stock_reserved_milli"), F("stock_rejected_reserved_milli"),
                                               function="stock_integer_subtract", output_field=CharField()),
               stock_available_milli=Func(F("stock_validated_milli"), F("stock_reserved_milli"),
                                         function="stock_integer_subtract", output_field=CharField())
    ).annotate(stock_low=Func(F("stock_available_milli"), F("stock_threshold_milli"),
                             function="stock_at_threshold", output_field=BooleanField()))
