"""Read a non-sellable receipt's remaining physical quantity from its decisions."""
from django.db.models import CharField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from stock.models import NonSellableDecision
from stock.services.stock_queries import StockTotal


def with_unsellable_balance(queryset):
    used = NonSellableDecision.objects.filter(receipt_id=OuterRef("pk")).order_by().values("receipt_id").annotate(total=StockTotal("quantity_milli")).values("total")
    return queryset.annotate(decided_quantity_milli=Coalesce(Subquery(used), Value("0"), output_field=CharField()))


def remaining_unsellable(receipt):
    if receipt.condition != "unsellable":
        return 0
    used = getattr(receipt, "decided_quantity_milli", None)
    if used is None:
        used = sum(receipt.decisions.values_list("quantity_milli", flat=True).iterator(chunk_size=1000))
    return receipt.quantity_milli - int(used)
