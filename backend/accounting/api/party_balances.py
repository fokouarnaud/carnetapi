"""Customer and supplier balances at a closing date for the accountant, read from the immutable
statement ledgers: validated amounts apart from pending ones, never compensated between parties."""
from datetime import timedelta

from django.db import transaction
from django.db.models import F, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.response import Response
from rest_framework.utils.urls import replace_query_param

from accounting.api.periods import utc_bounds
from accounting.models import ACCOUNTS_NOTE, suggestions
from common.api.fields import StrictSerializer
from common.services.operations import current_shop, require_manager
from customers.models import CustomerLedgerEntry
from identity.services.authentication import require_live_session
from purchasing.models import SupplierLedgerEntry


class BalancesQuerySerializer(StrictSerializer):
    party = serializers.ChoiceField(choices=["customers", "suppliers"])
    date = serializers.DateField()
    as_of = serializers.DateTimeField(required=False)
    page = serializers.IntegerField(min_value=1, required=False)
    page_size = serializers.IntegerField(min_value=1, required=False)

    def validate_as_of(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("Instantané futur non autorisé.")
        return value


class BalanceRowSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    debt_fcfa = serializers.IntegerField(help_text="What the party still owes (customer) or is owed (supplier).")
    credit_fcfa = serializers.IntegerField(help_text="Customer credit, or supplier advance plus credit.")
    pending_fcfa = serializers.IntegerField(help_text="Effect of operations still awaiting validation.")
    account = serializers.CharField(allow_null=True)
    account_label = serializers.CharField(allow_null=True)
    credit_account = serializers.CharField(allow_null=True)
    credit_account_label = serializers.CharField(allow_null=True)


class BalanceTotalsSerializer(serializers.Serializer):
    debt_fcfa = serializers.IntegerField()
    credit_fcfa = serializers.IntegerField()
    pending_fcfa = serializers.IntegerField()


class BalancesSerializer(serializers.Serializer):
    shop_name = serializers.CharField()
    party = serializers.CharField()
    date = serializers.DateField()
    timezone = serializers.CharField()
    as_of = serializers.DateTimeField()
    accounts_note = serializers.CharField()
    totals = BalanceTotalsSerializer()
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = BalanceRowSerializer(many=True)


def _grouped(party, shop, end, cutoff):
    validated = Q(state="validated")
    pending = Q(state="pending")
    if party == "customers":
        rows, owner, credit = CustomerLedgerEntry.objects, "customer", Sum("credit_delta_fcfa", filter=validated)
    else:
        rows, owner = SupplierLedgerEntry.objects, "supplier"
        credit = Sum(F("advance_delta_fcfa") + F("credit_delta_fcfa"), filter=validated)
    return (rows.filter(shop=shop, occurred_at__lt=end, recorded_at__lte=cutoff)
            .values(party_id=F(f"{owner}_id"), party_name=F(f"{owner}__name"))
            .annotate(debt=Coalesce(Sum("debt_delta_fcfa", filter=validated), 0),
                      credit=Coalesce(credit, 0),
                      pending=Coalesce(Sum("delta_fcfa", filter=pending), 0))
            .exclude(debt=0, credit=0, pending=0)
            .order_by("party_name", "party_id"))


class PartyBalancesViewSet(viewsets.GenericViewSet):
    queryset = CustomerLedgerEntry.objects.none()

    @extend_schema(parameters=[BalancesQuerySerializer], responses={200: BalancesSerializer}, description=(
        "Accountant export (16 November 2026): what each customer owes, or each supplier is owed, at the end of "
        "a local closing day, with credits and advances apart and pending operations apart, plus suggested "
        "SYSCOHADA accounts to be validated by the accountant. Parties with nothing open are left out; totals "
        "cover the whole filter. Managers only."))
    @transaction.atomic
    def list(self, request, *args, **kwargs):
        require_live_session(request.user, request.device_session.id, request.device_session.installation)
        shop = current_shop(self.kwargs["shop_id"], request.user, request.device_session.installation)
        require_manager(shop, request.user)
        query = BalancesQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        party, day = query.validated_data["party"], query.validated_data["date"]
        _begin, end = utc_bounds(shop, day, day)
        cutoff = query.validated_data.get("as_of", timezone.now())
        grouped = _grouped(party, shop, end, cutoff)
        totals = {"debt_fcfa": 0, "credit_fcfa": 0, "pending_fcfa": 0}
        for row in grouped.values("debt", "credit", "pending").iterator(chunk_size=1000):
            totals["debt_fcfa"] += row["debt"]
            totals["credit_fcfa"] += row["credit"]
            totals["pending_fcfa"] += row["pending"]
        accounts = suggestions()
        own_key, credit_key = ("customers", "customer_credits") if party == "customers" else ("suppliers", "supplier_advances")
        own, own_label = accounts.get(own_key, (None, None))
        other, other_label = accounts.get(credit_key, (None, None))
        lines = [{"id": row["party_id"], "name": row["party_name"], "debt_fcfa": row["debt"],
                  "credit_fcfa": row["credit"], "pending_fcfa": row["pending"],
                  "account": own, "account_label": own_label, "credit_account": other, "credit_account_label": other_label}
                 for row in self.paginate_queryset(grouped)]
        stamp = serializers.DateTimeField().to_representation(cutoff)

        def snapshot(link):
            return replace_query_param(link, "as_of", stamp) if link else None

        return Response(BalancesSerializer({
            "shop_name": shop.name, "party": party, "date": day, "timezone": shop.timezone, "as_of": cutoff,
            "accounts_note": ACCOUNTS_NOTE, "totals": totals, "count": self.paginator.page.paginator.count,
            "next": snapshot(self.paginator.get_next_link()), "previous": snapshot(self.paginator.get_previous_link()),
            "results": lines,
        }).data)
