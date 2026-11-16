"""The sales and purchases journal for the accountant: sales, customer returns, validated supplier
invoices and accepted supplier credits of a period, in date order, with suggested SYSCOHADA accounts.

Payments are not repeated here: they belong to the cash journal. Orders are forecasts and stay out.
"""
from django.db import transaction
from django.db.models import BooleanField, Case, CharField, ExpressionWrapper, F, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.response import Response
from rest_framework.utils.urls import replace_query_param

from accounting.api.cash_journal import PeriodSerializer
from accounting.api.periods import PeriodQuerySerializer, local_date, utc_bounds
from accounting.models import ACCOUNTS_NOTE, suggestions
from common.services.operations import current_shop, require_manager
from identity.services.authentication import require_live_session
from purchasing.models import PurchaseDocument, SupplierCredit
from sales.models import Sale, SaleReturn

COLUMNS = ["j_id", "j_at", "j_created", "j_journal", "j_kind", "j_amount", "j_gross", "j_discount",
           "j_party", "j_reference", "j_has_party"]


def _text(value):
    return Value(value, output_field=CharField())


def _entries(shop, begin, finish, cutoff):
    period = {"occurred_at__gte": begin, "occurred_at__lt": finish}
    sales = Sale.objects.filter(shop=shop, operation__created_at__lte=cutoff, **period).annotate(
        j_id=F("id"), j_at=F("occurred_at"), j_created=F("operation__created_at"), j_journal=_text("sales"),
        j_kind=Case(When(mode="credit", then=_text("sale_credit")), default=_text("sale_cash")),
        j_amount=F("net_total_fcfa"), j_gross=F("reference_total_fcfa"), j_discount=F("discount_total_fcfa"),
        j_party=Coalesce(F("customer__name"), _text("")), j_reference=_text(""),
        j_has_party=ExpressionWrapper(Q(mode="credit"), output_field=BooleanField()),
    ).order_by().values(*COLUMNS)
    returns = SaleReturn.objects.filter(sale__shop=shop, operation__created_at__lte=cutoff, **period).annotate(
        j_id=F("id"), j_at=F("occurred_at"), j_created=F("operation__created_at"), j_journal=_text("sales"),
        j_kind=_text("sale_return"), j_amount=-F("amount_fcfa"), j_gross=-F("amount_fcfa"), j_discount=Value(0),
        j_party=Coalesce(F("sale__customer__name"), _text("")), j_reference=_text(""),
        j_has_party=ExpressionWrapper(Q(sale__customer__isnull=False), output_field=BooleanField()),
    ).order_by().values(*COLUMNS)
    invoices = PurchaseDocument.objects.filter(shop=shop, kind="invoice", operation__created_at__lte=cutoff,
                                               **period).annotate(
        j_id=F("id"), j_at=F("occurred_at"), j_created=F("operation__created_at"), j_journal=_text("purchases"),
        j_kind=_text("purchase_invoice"), j_amount=F("total_fcfa"), j_gross=F("total_fcfa"), j_discount=Value(0),
        j_party=F("supplier__name"), j_reference=F("external_reference"),
        j_has_party=Value(True, output_field=BooleanField()),
    ).order_by().values(*COLUMNS)
    credits = SupplierCredit.objects.filter(supplier_return__document__shop=shop, kind=SupplierCredit.Kind.RETURN,
                                            operation__created_at__lte=cutoff, **period).annotate(
        j_id=F("id"), j_at=F("occurred_at"), j_created=F("operation__created_at"), j_journal=_text("purchases"),
        j_kind=_text("supplier_credit"), j_amount=-F("amount_fcfa"), j_gross=-F("amount_fcfa"), j_discount=Value(0),
        j_party=F("supplier_return__document__supplier__name"), j_reference=F("supplier_return__document__external_reference"),
        j_has_party=Value(True, output_field=BooleanField()),
    ).order_by().values(*COLUMNS)
    totals = {
        "sales": sales.aggregate(total=Sum("net_total_fcfa"))["total"] or 0,
        "sale_returns": returns.aggregate(total=Sum("amount_fcfa"))["total"] or 0,
        "purchases": invoices.aggregate(total=Sum("total_fcfa"))["total"] or 0,
        "supplier_credits": credits.aggregate(total=Sum("amount_fcfa"))["total"] or 0,
    }
    totals["net_sales"] = totals["sales"] - totals["sale_returns"]
    totals["net_purchases"] = totals["purchases"] - totals["supplier_credits"]
    union = sales.union(returns, invoices, credits, all=True).order_by("j_at", "j_created", "j_id")
    return union, totals


class JournalEntrySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    occurred_at = serializers.DateTimeField()
    local_date = serializers.CharField()
    journal = serializers.ChoiceField(choices=["sales", "purchases"])
    kind = serializers.ChoiceField(choices=["sale_cash", "sale_credit", "sale_return", "purchase_invoice", "supplier_credit"])
    amount_fcfa = serializers.IntegerField()
    gross_fcfa = serializers.IntegerField()
    discount_fcfa = serializers.IntegerField()
    party_name = serializers.CharField(allow_blank=True)
    reference = serializers.CharField(allow_blank=True)
    account = serializers.CharField(allow_null=True)
    account_label = serializers.CharField(allow_null=True)
    party_account = serializers.CharField(allow_null=True)
    party_account_label = serializers.CharField(allow_null=True)


class JournalTotalsSerializer(serializers.Serializer):
    sales = serializers.IntegerField()
    sale_returns = serializers.IntegerField()
    net_sales = serializers.IntegerField()
    purchases = serializers.IntegerField()
    supplier_credits = serializers.IntegerField()
    net_purchases = serializers.IntegerField()


class SalesPurchasesJournalSerializer(serializers.Serializer):
    shop_name = serializers.CharField()
    period = PeriodSerializer()
    as_of = serializers.DateTimeField()
    accounts_note = serializers.CharField()
    totals = JournalTotalsSerializer()
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = JournalEntrySerializer(many=True)


class SalesPurchasesJournalViewSet(viewsets.GenericViewSet):
    queryset = Sale.objects.none()

    @extend_schema(parameters=[PeriodQuerySerializer], responses={200: SalesPurchasesJournalSerializer}, description=(
        "Accountant export (16 November 2026): sales, customer returns, validated supplier invoices and accepted "
        "supplier credits between two local days, both included, in date order, with totals over the whole "
        "period and suggested SYSCOHADA accounts to be validated by the accountant. Payments belong to the cash "
        "journal; orders stay out. Managers only; SQL pages share the same as_of snapshot."))
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
        union, totals = _entries(shop, begin, finish, cutoff)
        accounts = suggestions()

        def account(key):
            return accounts.get(key, (None, None))

        lines = []
        for row in self.paginate_queryset(union):
            own, own_label = account("sales" if row["j_journal"] == "sales" else "purchases")
            party, party_label = (account("customers" if row["j_journal"] == "sales" else "suppliers")
                                  if row["j_has_party"] else (None, None))
            lines.append({
                "id": row["j_id"], "occurred_at": row["j_at"], "local_date": local_date(shop, row["j_at"]),
                "journal": row["j_journal"], "kind": row["j_kind"], "amount_fcfa": row["j_amount"],
                "gross_fcfa": row["j_gross"], "discount_fcfa": row["j_discount"], "party_name": row["j_party"],
                "reference": row["j_reference"], "account": own, "account_label": own_label,
                "party_account": party, "party_account_label": party_label,
            })
        stamp = serializers.DateTimeField().to_representation(cutoff)

        def snapshot(link):
            return replace_query_param(link, "as_of", stamp) if link else None

        return Response(SalesPurchasesJournalSerializer({
            "shop_name": shop.name,
            "period": {"start": start, "end": end, "timezone": shop.timezone, "start_utc": begin, "end_utc": finish},
            "as_of": cutoff, "accounts_note": ACCOUNTS_NOTE, "totals": totals,
            "count": self.paginator.page.paginator.count, "next": snapshot(self.paginator.get_next_link()),
            "previous": snapshot(self.paginator.get_previous_link()), "results": lines,
        }).data)
