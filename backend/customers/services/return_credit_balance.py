"""Read remaining return credit from immutable settlement events."""
from django.db.models import CharField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from customers.models import ReturnCreditSettlement
from stock.services.stock_queries import StockTotal


def with_return_credit(queryset):
    used = ReturnCreditSettlement.objects.filter(sale_return_id=OuterRef("pk")).order_by().values("sale_return_id").annotate(total=StockTotal("amount_fcfa")).values("total")
    return queryset.annotate(used_credit_fcfa=Coalesce(Subquery(used), Value("0"), output_field=CharField()))


def remaining_return_credit(returned):
    used = getattr(returned, "used_credit_fcfa", None)
    if used is None:
        used = sum(returned.settlements.values_list("amount_fcfa", flat=True).iterator(chunk_size=1000))
    return returned.credit_fcfa - int(used)
