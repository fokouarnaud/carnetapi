"""The cash journal for the accountant: real money movements of a period, by means of payment,
with a suggested SYSCOHADA account for each side; till counts and their gaps listed apart."""
from django.db import transaction
from django.db.models import Case, CharField, F, When
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.response import Response
from rest_framework.utils.urls import replace_query_param

from accounting.api.periods import PeriodQuerySerializer, local_date, utc_bounds
from accounting.models import ACCOUNTS_NOTE, suggestions
from cash.api.cash_counts import expected_cash_at
from cash.models import CashCount, CashMovement
from common.services.operations import current_shop, require_manager
from identity.services.authentication import require_live_session


def _with_names(rows):
    return rows.annotate(payment_name=Case(
        When(sale_payment__isnull=False, then=F("sale_payment__other_name")),
        When(customer_payment_part__isnull=False, then=F("customer_payment_part__other_name")),
        When(customer_refund__isnull=False, then=F("customer_refund__other_name")),
        When(supplier_payment_part__isnull=False, then=F("supplier_payment_part__other_name")),
        When(supplier_refund__isnull=False, then=F("supplier_refund__other_name")),
        When(sale_return_refund__isnull=False, then=F("sale_return_refund__other_name")),
        default=F("other_name"), output_field=CharField(),
    ))


def _method_key(method, name):
    return "other:" + name if method == "other" else method


def _balances(rows):
    totals = {}
    for method, name, amount in rows.values_list("method", "payment_name", "amount_fcfa").iterator(chunk_size=1000):
        key = _method_key(method, name)
        totals[key] = totals.get(key, 0) + amount
    return totals


def _kind(row):
    if row.manual_kind:
        return row.manual_kind
    for field, kind in [("sale_payment_id", "sale_payment"), ("customer_payment_part_id", "customer_collection"),
                        ("customer_refund_id", "customer_refund"), ("supplier_payment_part_id", "supplier_payment"),
                        ("supplier_refund_id", "supplier_refund"), ("sale_return_refund_id", "sale_return_refund")]:
        if getattr(row, field):
            return kind
    return "unknown"


class JournalLineSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    occurred_at = serializers.DateTimeField()
    local_date = serializers.CharField()
    kind = serializers.CharField()
    method = serializers.CharField()
    reason = serializers.CharField()
    external_reference = serializers.CharField()
    amount_in = serializers.IntegerField()
    amount_out = serializers.IntegerField()
    account = serializers.CharField(allow_null=True)
    account_label = serializers.CharField(allow_null=True)
    counter_account = serializers.CharField(allow_null=True)
    counter_label = serializers.CharField(allow_null=True)


class CountGapSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    counted_at = serializers.DateTimeField()
    local_date = serializers.CharField()
    method = serializers.CharField()
    expected_fcfa = serializers.IntegerField()
    physical_fcfa = serializers.IntegerField()
    difference_fcfa = serializers.IntegerField()
    account = serializers.CharField(allow_null=True)
    account_label = serializers.CharField(allow_null=True)


class InOutSerializer(serializers.Serializer):
    in_ = serializers.IntegerField()
    out = serializers.IntegerField()

    def to_representation(self, instance):
        return {"in": instance["in"], "out": instance["out"]}


class PeriodSerializer(serializers.Serializer):
    start = serializers.DateField()
    end = serializers.DateField()
    timezone = serializers.CharField()
    start_utc = serializers.DateTimeField()
    end_utc = serializers.DateTimeField()


class CashJournalSerializer(serializers.Serializer):
    shop_name = serializers.CharField()
    period = PeriodSerializer()
    as_of = serializers.DateTimeField()
    accounts_note = serializers.CharField()
    opening_balance_by_method = serializers.DictField(child=serializers.IntegerField())
    totals_by_method = serializers.DictField(child=InOutSerializer())
    closing_balance_by_method = serializers.DictField(child=serializers.IntegerField())
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = JournalLineSerializer(many=True)
    cash_counts = CountGapSerializer(many=True)


class CashJournalViewSet(viewsets.GenericViewSet):
    queryset = CashMovement.objects.none()

    @extend_schema(parameters=[PeriodQuerySerializer], responses={200: CashJournalSerializer}, description=(
        "Accountant export (16 November 2026): real money movements of the shop between two local days, "
        "both included, with opening balance, in/out totals and closing balance per means of payment over "
        "the whole period, and a suggested SYSCOHADA account per side, to be validated by the accountant. "
        "Till counts of the period and their current gaps are listed apart. Managers only; SQL pages share "
        "the same as_of snapshot."))
    @transaction.atomic
    def list(self, request, *args, **kwargs):
        require_live_session(request.user, request.device_session.id, request.device_session.installation)
        shop = current_shop(self.kwargs["shop_id"], request.user, request.device_session.installation)
        require_manager(shop, request.user)
        query = PeriodQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        start, end = query.validated_data["start"], query.validated_data["end"]
        begin, finish = utc_bounds(shop, start, end)
        cutoff = query.validated_data.get("as_of", timezone.now())
        known = _with_names(CashMovement.objects.filter(shop=shop, operation__created_at__lte=cutoff))
        opening = _balances(known.filter(occurred_at__lt=begin))
        period = known.filter(occurred_at__gte=begin, occurred_at__lt=finish)
        totals = {}
        for method, name, amount in period.values_list("method", "payment_name", "amount_fcfa").iterator(chunk_size=1000):
            side = totals.setdefault(_method_key(method, name), {"in": 0, "out": 0})
            side["in" if amount > 0 else "out"] += abs(amount)
        closing = dict(opening)
        for key, side in totals.items():
            closing[key] = closing.get(key, 0) + side["in"] - side["out"]
        accounts = suggestions()

        def account(key):
            return accounts.get(key, (None, None))

        page = self.paginate_queryset(period.order_by("occurred_at", "operation__created_at", "id"))
        lines = []
        for row in page:
            kind = _kind(row)
            counter_key = f"expense:{row.expense_category or 'other'}" if kind == "expense" else kind
            own, own_label = account(f"method:{row.method}")
            counter, counter_label = account(counter_key)
            lines.append({
                "id": row.id, "occurred_at": row.occurred_at, "local_date": local_date(shop, row.occurred_at),
                "kind": kind, "method": _method_key(row.method, row.payment_name), "reason": row.reason,
                "external_reference": row.external_reference,
                "amount_in": max(row.amount_fcfa, 0), "amount_out": max(-row.amount_fcfa, 0),
                "account": own, "account_label": own_label, "counter_account": counter, "counter_label": counter_label,
            })
        counts = []
        for count in CashCount.objects.filter(shop=shop, created_at__lte=cutoff, counted_at__gte=begin,
                                              counted_at__lt=finish).order_by("counted_at", "id")[:500]:
            expected = expected_cash_at(shop.id, count.method, count.other_name, count.counted_at)
            difference = count.physical_fcfa - expected
            gap_account, gap_label = (account("count_shortage") if difference < 0 else
                                      account("count_surplus") if difference > 0 else (None, None))
            counts.append({"id": count.id, "counted_at": count.counted_at, "local_date": local_date(shop, count.counted_at),
                           "method": _method_key(count.method, count.other_name), "expected_fcfa": expected,
                           "physical_fcfa": count.physical_fcfa, "difference_fcfa": difference,
                           "account": gap_account, "account_label": gap_label})
        stamp = serializers.DateTimeField().to_representation(cutoff)

        def snapshot(link):
            return replace_query_param(link, "as_of", stamp) if link else None

        return Response(CashJournalSerializer({
            "shop_name": shop.name,
            "period": {"start": start, "end": end, "timezone": shop.timezone, "start_utc": begin, "end_utc": finish},
            "as_of": cutoff, "accounts_note": ACCOUNTS_NOTE,
            "opening_balance_by_method": opening, "totals_by_method": totals, "closing_balance_by_method": closing,
            "count": self.paginator.page.paginator.count, "next": snapshot(self.paginator.get_next_link()),
            "previous": snapshot(self.paginator.get_previous_link()), "results": lines, "cash_counts": counts,
        }).data)
