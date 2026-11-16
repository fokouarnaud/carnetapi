"""Stock valued at moving average cost at the end of a period, for the accountant: an unknown cost
stays unknown (never zero), provisional values are flagged, and the period's losses are listed apart."""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.response import Response

from accounting.api.cash_journal import PeriodSerializer
from accounting.api.periods import PeriodQuerySerializer, local_date, utc_bounds
from accounting.models import ACCOUNTS_NOTE, suggestions
from catalogue.models import Product
from common.services.operations import current_shop, require_manager
from identity.services.authentication import require_live_session
from stock.api.stock_valuations import current_valuation
from stock.models import StockLoss

NANOS = 1_000_000_000


def _round(numerator, denominator):
    """Nearest FCFA, half up, on non-negative exact integers."""
    return (2 * numerator + denominator) // (2 * denominator)


def _fcfa(nanos):
    if nanos is None:
        return None
    return _round(nanos, NANOS) if nanos >= 0 else -_round(-nanos, NANOS)


def _value(product, until):
    state = current_valuation(product, at=until).state
    value = state.stock_value_nanos_fcfa
    status = ("unknown" if value is None else
              "provisional" if state.quantity_milli > 0 and state.bucket_cost_is_provisional else "known")
    return {"id": product.id, "name": product.name, "unit_label": product.unit_label,
            "quantity_milli": str(state.quantity_milli), "value_fcfa": _fcfa(value), "value_state": status,
            "requires_regularization": state.has_unresolved_valuation or state.has_unresolved_shortage}


def _loss_value(loss):
    """The loss at the moving average cost just before it; unknown when that cost is unknown."""
    state = current_valuation(loss.product, at=loss.occurred_at - timedelta(microseconds=1)).state
    value, quantity = state.stock_value_nanos_fcfa, state.quantity_milli
    if value is None or quantity <= 0 or state.bucket_cost_is_provisional:
        return None
    return _round(value * loss.quantity_milli, quantity * NANOS)


class StockRowSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    unit_label = serializers.CharField()
    quantity_milli = serializers.CharField()
    value_fcfa = serializers.IntegerField(allow_null=True)
    value_state = serializers.ChoiceField(choices=["known", "provisional", "unknown"])
    requires_regularization = serializers.BooleanField()
    account = serializers.CharField(allow_null=True)
    account_label = serializers.CharField(allow_null=True)


class LossRowSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    occurred_at = serializers.DateTimeField()
    local_date = serializers.CharField()
    name = serializers.CharField()
    quantity_milli = serializers.CharField()
    value_fcfa = serializers.IntegerField(allow_null=True)
    reason = serializers.CharField()
    account = serializers.CharField(allow_null=True)
    account_label = serializers.CharField(allow_null=True)


class StockTotalsSerializer(serializers.Serializer):
    known_value_fcfa = serializers.IntegerField()
    provisional_count = serializers.IntegerField()
    unknown_count = serializers.IntegerField()
    losses_value_fcfa = serializers.IntegerField()
    losses_unknown_count = serializers.IntegerField()


class StockExportSerializer(serializers.Serializer):
    shop_name = serializers.CharField()
    period = PeriodSerializer()
    valued_at = serializers.DateTimeField()
    accounts_note = serializers.CharField()
    totals = StockTotalsSerializer()
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = StockRowSerializer(many=True)
    losses = LossRowSerializer(many=True)


class StockExportViewSet(viewsets.GenericViewSet):
    queryset = Product.objects.none()

    @extend_schema(parameters=[PeriodQuerySerializer], responses={200: StockExportSerializer}, description=(
        "Accountant export (16 November 2026): each product with stock history, valued at moving average cost "
        "at the end of the last local day; unknown costs stay null and are counted, provisional values are "
        "flagged. Losses of the period are listed apart with their frozen cost. Suggested SYSCOHADA accounts "
        "are to be validated by the accountant. Totals cover every product. Managers only."))
    @transaction.atomic
    def list(self, request, *args, **kwargs):
        require_live_session(request.user, request.device_session.id, request.device_session.installation)
        shop = current_shop(self.kwargs["shop_id"], request.user, request.device_session.installation)
        require_manager(shop, request.user)
        query = PeriodQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        start, end = query.validated_data["start"], query.validated_data["end"]
        begin, finish = utc_bounds(shop, start, end)
        until = min(finish - timedelta(microseconds=1), timezone.now())
        products = (Product.objects.filter(shop=shop, stock_movements__occurred_at__lt=finish)
                    .distinct().order_by("name", "id"))
        accounts = suggestions()
        stock_account, stock_label = accounts.get("stock", (None, None))
        loss_account, loss_label = accounts.get("stock_loss", (None, None))
        totals = {"known_value_fcfa": 0, "provisional_count": 0, "unknown_count": 0,
                  "losses_value_fcfa": 0, "losses_unknown_count": 0}
        # Totals cover every product; to measure on the pilot data set (DOD-08).
        values = {}
        for product in products.iterator(chunk_size=200):
            row = _value(product, until)
            values[product.id] = row
            if row["value_state"] == "unknown":
                totals["unknown_count"] += 1
            else:
                totals["known_value_fcfa"] += row["value_fcfa"]
                totals["provisional_count"] += row["value_state"] == "provisional"
        lines = [{**values[product.id], "account": stock_account, "account_label": stock_label}
                 for product in self.paginate_queryset(products)]
        losses = []
        for loss in (StockLoss.objects.filter(product__shop=shop, occurred_at__gte=begin, occurred_at__lt=finish)
                     .select_related("product", "operation").order_by("occurred_at", "id")[:2000]):
            value = _loss_value(loss)
            if value is None:
                totals["losses_unknown_count"] += 1
            else:
                totals["losses_value_fcfa"] += value
            losses.append({"id": loss.id, "occurred_at": loss.occurred_at, "local_date": local_date(shop, loss.occurred_at),
                           "name": loss.product.name, "quantity_milli": str(loss.quantity_milli), "value_fcfa": value,
                           "reason": loss.operation.reason, "account": loss_account, "account_label": loss_label})
        return Response(StockExportSerializer({
            "shop_name": shop.name,
            "period": {"start": start, "end": end, "timezone": shop.timezone, "start_utc": begin, "end_utc": finish},
            "valued_at": until, "accounts_note": ACCOUNTS_NOTE, "totals": totals,
            "count": self.paginator.page.paginator.count, "next": self.paginator.get_next_link(),
            "previous": self.paginator.get_previous_link(), "results": lines, "losses": losses,
        }).data)
