"""SQL-paged statement rows and exact Python integer totals at a fixed cutoff."""
from collections import defaultdict
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.utils.urls import replace_query_param

from identity.services.authentication import require_live_session
from common.api.fields import StrictSerializer
from common.services.dates import due_at_utc
from customers.services.debt_services import manager_debt_context
from customers.models import CustomerLedgerEntry, DebtAllocation, RegularizationAllocation, ReturnCreditAllocation
from backups.api.recovery_metadata import RecoveryReportSerializer
from common.services.origins import with_public_origin
from common.api.errors import OriginReferenceError


class StatementRequestSerializer(StrictSerializer):
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    as_of = serializers.DateTimeField(required=False)
    page = serializers.IntegerField(min_value=1, required=False)
    page_size = serializers.IntegerField(min_value=1, required=False)

    def validate(self, data):
        if data.get("date_from") and data.get("date_to") and data["date_from"] > data["date_to"]:
            raise serializers.ValidationError("Début de période après sa fin.")
        if data.get("as_of") and data["as_of"] > timezone.now():
            raise serializers.ValidationError("Instantané futur non autorisé.")
        return data


class StatementAllocationSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    debt_id = serializers.UUIDField()
    requested_amount_fcfa = serializers.IntegerField()
    applied_amount_fcfa = serializers.IntegerField()


class StatementEntrySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    reference_id = serializers.UUIDField(source="source_id")
    operation_id = serializers.UUIDField()
    actor_id = serializers.UUIDField(source="public_actor_id")
    reason = serializers.CharField(source="operation.reason")
    kind = serializers.CharField()
    state = serializers.CharField()
    occurred_at = serializers.DateTimeField()
    recorded_at = serializers.DateTimeField()
    delta_fcfa = serializers.IntegerField()
    balance_after_fcfa = serializers.IntegerField()
    allocations = StatementAllocationSerializer(many=True)


class StatementResponseSerializer(RecoveryReportSerializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    as_of = serializers.DateTimeField()
    opening_balance_fcfa = serializers.IntegerField()
    closing_balance_fcfa = serializers.IntegerField()
    closing_debt_fcfa = serializers.IntegerField()
    closing_unallocated_fcfa = serializers.IntegerField()
    pending_count = serializers.IntegerField()
    pending_delta_fcfa = serializers.IntegerField()
    provisional = serializers.BooleanField()
    results = StatementEntrySerializer(many=True)


def statement_totals(rows, page_ids, start):
    balance = debt = credit = opening = pending_count = pending_delta = 0
    balances = {}
    for row in rows.values("id", "occurred_at", "state", "delta_fcfa", "debt_delta_fcfa", "credit_delta_fcfa").iterator(chunk_size=1000):
        in_period = start is None or row["occurred_at"] >= start
        if row["state"] == "validated":
            balance += row["delta_fcfa"]
            debt += row["debt_delta_fcfa"]
            credit += row["credit_delta_fcfa"]
            if not in_period:
                opening = balance
        elif row["state"] == "pending" and in_period:
            pending_count += 1
            pending_delta += row["delta_fcfa"]
        if row["id"] in page_ids:
            balances[row["id"]] = balance
    return {"opening_balance_fcfa": opening, "closing_balance_fcfa": balance,
            "closing_debt_fcfa": debt, "closing_unallocated_fcfa": credit,
            "pending_count": pending_count, "pending_delta_fcfa": pending_delta}, balances


def page_allocations(page, shop, customer):
    allocations = defaultdict(list)
    collections = [row.source_id for row in page if row.kind == "collection"]
    decisions = [row.source_id for row in page if row.kind == "allocation"]
    for row in DebtAllocation.objects.filter(payment_id__in=collections, payment__shop=shop, payment__customer=customer).order_by("position").values(
        "id", "debt_id", "payment_id", "requested_amount_fcfa", "applied_amount_fcfa",
    ):
        parent_id = row.pop("payment_id")
        allocations[parent_id].append(row)
    source = (Q(regularization__payment__shop=shop, regularization__payment__customer=customer)
              | Q(regularization__credit__shop=shop, regularization__credit__customer=customer))
    for row in RegularizationAllocation.objects.filter(source, regularization_id__in=decisions).order_by("position").values(
        "id", "debt_id", "regularization_id", "amount_fcfa",
    ):
        parent_id = row.pop("regularization_id")
        amount = row.pop("amount_fcfa")
        allocations[parent_id].append({**row, "requested_amount_fcfa": amount, "applied_amount_fcfa": amount})
    return_decisions = [row.source_id for row in page if row.kind == "return_credit_allocation"]
    for row in ReturnCreditAllocation.objects.filter(settlement_id__in=return_decisions,
            settlement__sale_return__sale__shop=shop, settlement__sale_return__sale__customer=customer).order_by("created_at", "id").values(
                "id", "debt_id", "settlement_id", "amount_fcfa"):
        parent_id = row.pop("settlement_id")
        amount = row.pop("amount_fcfa")
        allocations[parent_id].append({**row, "requested_amount_fcfa": amount, "applied_amount_fcfa": amount})
    return allocations


class CustomerStatementViewSet(viewsets.GenericViewSet):
    queryset = CustomerLedgerEntry.objects.none()
    serializer_class = StatementEntrySerializer

    @extend_schema(parameters=[StatementRequestSerializer], responses={200:StatementResponseSerializer,409:OriginReferenceError},description=(
        "Current shop responsible reads a customer statement after live session and object authorization. "
        "Rows are paged in SQL with an as_of cutoff retained by page links; totals cover the full filter. "
        "provisional stays true while device completeness is unproven. restoration describes the current "
        "authorized shop's recovery, independently of as_of: null for no current restore, known_devices null "
        "for an unrecorded inventory, and zero observations never proves complete recovery. No device identities are exposed."))
    @transaction.atomic
    def list(self, request, *args, **kwargs):
        require_live_session(request.user, request.device_session.id, request.device_session.installation)
        shop, customer = manager_debt_context(self.kwargs["shop_id"], self.kwargs["client_id"], request.user, request.device_session.installation)
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
        rows = with_public_origin(CustomerLedgerEntry.objects.filter(shop=shop, customer=customer, recorded_at__lte=cutoff),
            actor_path="operation__actor_id",installation_path=None)
        if end is not None:
            rows = rows.filter(occurred_at__lt=end)
        period = rows.filter(occurred_at__gte=start) if start is not None else rows
        page = self.paginate_queryset(period.select_related("operation").defer("operation__details"))
        totals, balances = statement_totals(rows, {row.id for row in page}, start)
        allocations = page_allocations(page, shop, customer)
        for row in page:
            row.balance_after_fcfa = balances[row.id]
            row.allocations = allocations[row.source_id]
        timestamp = serializers.DateTimeField().to_representation(cutoff)
        next_link = self.paginator.get_next_link()
        previous_link = self.paginator.get_previous_link()
        return Response(StatementResponseSerializer({
            "count": self.paginator.page.paginator.count,
            "next": replace_query_param(next_link, "as_of", timestamp) if next_link else None,
            "previous": replace_query_param(previous_link, "as_of", timestamp) if previous_link else None,
            "as_of": cutoff, **totals,
            # Devices' missing operations cannot yet be proven absent by a sync checkpoint.
            "provisional": True, "results": page,
        },context={"shop":shop}).data)
