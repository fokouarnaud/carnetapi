"""Exact supplier statements, scoped before SQL pagination and fixed at a cutoff."""
from collections import defaultdict
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.utils.urls import replace_query_param
from common.services.dates import due_at_utc
from purchasing.models import SupplierLedgerEntry, SupplierPaymentAllocation, SupplierCreditAllocation, SupplierPaymentPart
from purchasing.api.purchases import context
from customers.api.statements import StatementRequestSerializer
from backups.api.recovery_metadata import RecoveryReportSerializer
from common.services.origins import with_public_origin
from common.api.errors import OriginReferenceError


class SupplierStatementAllocationSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    debt_id = serializers.UUIDField()
    amount_fcfa = serializers.IntegerField()
    payment_id = serializers.UUIDField(allow_null=True)
    credit_id = serializers.UUIDField(allow_null=True)


class SupplierStatementPartSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    method = serializers.CharField()
    amount_fcfa = serializers.IntegerField()
    other_name = serializers.CharField()
    external_reference = serializers.CharField()


class SupplierStatementEntrySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    reference_id = serializers.UUIDField(source="source_id")
    external_reference = serializers.CharField()
    operation_id = serializers.UUIDField()
    actor_id = serializers.UUIDField(source="public_actor_id")
    reason = serializers.CharField(source="operation.reason")
    kind = serializers.CharField()
    state = serializers.CharField()
    occurred_at = serializers.DateTimeField()
    recorded_at = serializers.DateTimeField()
    delta_fcfa = serializers.IntegerField()
    debt_delta_fcfa = serializers.IntegerField()
    advance_delta_fcfa = serializers.IntegerField()
    credit_delta_fcfa = serializers.IntegerField()
    balance_after_fcfa = serializers.IntegerField()
    allocations = SupplierStatementAllocationSerializer(many=True)
    parts = SupplierStatementPartSerializer(many=True)


class SupplierStatementResponseSerializer(RecoveryReportSerializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    as_of = serializers.DateTimeField()
    opening_balance_fcfa = serializers.IntegerField()
    closing_balance_fcfa = serializers.IntegerField()
    closing_debt_fcfa = serializers.IntegerField()
    closing_advance_fcfa = serializers.IntegerField()
    closing_credit_fcfa = serializers.IntegerField()
    pending_count = serializers.IntegerField()
    pending_delta_fcfa = serializers.IntegerField()
    pending_debt_delta_fcfa = serializers.IntegerField()
    pending_advance_delta_fcfa = serializers.IntegerField()
    pending_credit_delta_fcfa = serializers.IntegerField()
    provisional = serializers.BooleanField()
    results = SupplierStatementEntrySerializer(many=True)


def totals_for_page(rows, page_ids, start):
    balance = debt = advance = credit = opening = pending_count = pending_delta = 0
    pending_debt = pending_advance = pending_credit = 0
    balances = {}
    for row in rows.values("id", "occurred_at", "state", "delta_fcfa", "debt_delta_fcfa", "advance_delta_fcfa", "credit_delta_fcfa").iterator(chunk_size=1000):
        in_period = start is None or row["occurred_at"] >= start
        if row["state"] == "validated":
            balance += row["delta_fcfa"]
            debt += row["debt_delta_fcfa"]
            advance += row["advance_delta_fcfa"]
            credit += row["credit_delta_fcfa"]
            if not in_period:
                opening = balance
        elif row["state"] == "pending" and in_period:
            pending_count += 1
            pending_delta += row["delta_fcfa"]
            pending_debt += row["debt_delta_fcfa"]
            pending_advance += row["advance_delta_fcfa"]
            pending_credit += row["credit_delta_fcfa"]
        if row["id"] in page_ids:
            balances[row["id"]] = balance
    return {"opening_balance_fcfa": opening, "closing_balance_fcfa": balance, "closing_debt_fcfa": debt,
            "closing_advance_fcfa": advance, "closing_credit_fcfa": credit, "pending_count": pending_count,
            "pending_delta_fcfa": pending_delta, "pending_debt_delta_fcfa": pending_debt,
            "pending_advance_delta_fcfa": pending_advance, "pending_credit_delta_fcfa": pending_credit}, balances


def details_for_page(page, shop, supplier):
    allocations, parts = defaultdict(list), defaultdict(list)
    ids = [row.operation_id for row in page]
    for row in SupplierPaymentAllocation.objects.filter(operation_id__in=ids, payment__shop=shop,
            payment__supplier=supplier).order_by("position").values("id", "operation_id", "debt_id", "amount_fcfa", "payment_id"):
        allocations[row.pop("operation_id")].append({**row, "credit_id": None})
    from purchasing.api.supplier_credits import credits_for_supplier
    credits = credits_for_supplier(shop, supplier).values("id")
    for row in SupplierCreditAllocation.objects.filter(operation_id__in=ids, credit_id__in=credits).order_by("position").values(
            "id", "operation_id", "debt_id", "amount_fcfa", "credit_id"):
        allocations[row.pop("operation_id")].append({**row, "payment_id": None})
    payment_ids = [row.source_id for row in page if row.kind == "payment"]
    for row in SupplierPaymentPart.objects.filter(payment_id__in=payment_ids, payment__shop=shop,
            payment__supplier=supplier).order_by("position").values("id", "payment_id", "method", "amount_fcfa", "other_name", "external_reference"):
        parts[row.pop("payment_id")].append(row)
    return allocations, parts


class SupplierStatementViewSet(viewsets.GenericViewSet):
    queryset = SupplierLedgerEntry.objects.none()
    serializer_class = SupplierStatementEntrySerializer

    @extend_schema(parameters=[StatementRequestSerializer], responses={200:SupplierStatementResponseSerializer,409:OriginReferenceError},description=(
        "Current shop responsible reads a supplier statement after live session and object authorization. "
        "Debts, advances, credits, real payments and pending amounts remain distinct; totals cover the full filter. "
        "SQL pagination retains the as_of cutoff. provisional stays true while device completeness is unproven. "
        "restoration describes the current shop's recovery, independently of the financial cutoff, without device "
        "identities. No current restore is null; unknown inventory is not zero and zero never proves complete recovery."))
    @transaction.atomic
    def list(self, request, *args, **kwargs):
        shop, supplier = context(self.kwargs["shop_id"], self.kwargs["supplier_id"], request)
        params = StatementRequestSerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        values = params.validated_data
        cutoff = values.get("as_of", timezone.now())
        start = due_at_utc(values.get("date_from"), shop.timezone)
        try:
            end_date = values["date_to"] + timedelta(days=1) if "date_to" in values else None
        except OverflowError:
            raise ValidationError({"date_to": "Fin de période hors capacité."})
        end = due_at_utc(end_date, shop.timezone)
        rows = with_public_origin(SupplierLedgerEntry.objects.filter(shop=shop,supplier=supplier,recorded_at__lte=cutoff),
            actor_path='operation__actor_id',installation_path=None)
        if end is not None:
            rows = rows.filter(occurred_at__lt=end)
        period = rows.filter(occurred_at__gte=start) if start is not None else rows
        page = self.paginate_queryset(period.select_related("operation").defer("operation__details"))
        totals, balances = totals_for_page(rows, {row.id for row in page}, start)
        allocations, parts = details_for_page(page, shop, supplier)
        for row in page:
            row.balance_after_fcfa = balances[row.id]
            row.allocations = allocations[row.operation_id]
            row.parts = parts[row.source_id]
        timestamp = serializers.DateTimeField().to_representation(cutoff)
        next_link, previous_link = self.paginator.get_next_link(), self.paginator.get_previous_link()
        return Response(SupplierStatementResponseSerializer({"count": self.paginator.page.paginator.count,
            "next": replace_query_param(next_link, "as_of", timestamp) if next_link else None,
            "previous": replace_query_param(previous_link, "as_of", timestamp) if previous_link else None,
            "as_of": cutoff, **totals, "provisional": True, "results": page},context={"shop":shop}).data)
