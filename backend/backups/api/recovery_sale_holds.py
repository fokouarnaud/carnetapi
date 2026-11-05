"""Responsible review queues keep recovered money apart from validated ledgers."""
from django.db import transaction,models
from drf_spectacular.utils import extend_schema,extend_schema_serializer
from rest_framework import serializers,viewsets
from rest_framework.response import Response

from backups.api.recovery_integration import ERRORS
from backups.api.recovery_journals import RecoveryJournalParser
from backups.api.recovery_metadata import RecoveryPagination,RecoveryReportSerializer
from backups.models import RecoverySaleHold,RecoverySaleHoldDecision,RecoveryJournalOperation
from backups.services.recovery_sale_holds import RecoverySaleHoldInput,decide_sale_hold,find_sale_hold,hold_context
from backups.services.recovery_integration import _capture
from stock.services.stock_queries import StockTotal


class HoldReservationSerializer(serializers.Serializer):
    id=serializers.UUIDField(required=False,help_text="Absent from immutable receipts recorded before reconciliation support; current integration-preview exposes the ID.")
    product_id=serializers.UUIDField()
    quantity_milli=serializers.IntegerField(min_value=1)
    state=serializers.ChoiceField(choices=["pending","rejected","validated"])


@extend_schema_serializer(many=False)
class RecoverySaleHoldReceipt(serializers.Serializer):
    id=serializers.UUIDField()
    hold_id=serializers.UUIDField()
    capture_id=serializers.UUIDField()
    operation_id=serializers.UUIDField()
    source_operation_id=serializers.UUIDField()
    source_sale_id=serializers.UUIDField(required=False,help_text="Absent only from immutable receipts created before the sale-identity migration.")
    state=serializers.ChoiceField(choices=["pending","refused","validated"])
    version=serializers.IntegerField(min_value=1)
    reason=serializers.CharField()
    reviewed_by=serializers.UUIDField()
    reviewed_installation_id=serializers.UUIDField()
    reviewed_at=serializers.DateTimeField()
    integration_decision_id=serializers.UUIDField(allow_null=True)
    reservations=HoldReservationSerializer(many=True)
    net_total_fcfa=serializers.IntegerField(min_value=0)
    payments_fcfa=serializers.IntegerField(min_value=0)
    unpaid_debt_fcfa=serializers.IntegerField(min_value=0)
    customer_credit_fcfa=serializers.IntegerField(min_value=0)


class RecoverySaleReviewQuery(serializers.Serializer):
    state=serializers.ChoiceField(choices=["pending","refused","validated"],required=False)


class RecoverySaleHoldSummary(serializers.ModelSerializer):
    source_sale_id=serializers.UUIDField(source="sale_id")
    origin_capture_id=serializers.UUIDField(source="capture_id")
    current_capture_id=serializers.UUIDField(allow_null=True)
    origin_user_id=serializers.UUIDField(source="capture.origin_user_id")
    origin_installation_id=serializers.UUIDField(source="capture.origin_installation_id")
    occurred_at=serializers.DateTimeField(source="capture.occurred_at")
    class Meta:
        model=RecoverySaleHold
        fields=["id","source_operation_id","source_sale_id","origin_capture_id","current_capture_id","origin_user_id",
            "origin_installation_id","occurred_at","state","version","net_total_fcfa","payments_fcfa"]
        read_only_fields=fields


class ReviewMoneyTotals(serializers.Serializer):
    net_total_fcfa=serializers.CharField()
    payments_fcfa=serializers.CharField()
    unpaid_debt_fcfa=serializers.CharField()
    customer_credit_fcfa=serializers.CharField()


@extend_schema_serializer(many=False)
class RecoverySaleHoldPage(RecoveryReportSerializer):
    count=serializers.IntegerField()
    next=serializers.URLField(allow_null=True)
    previous=serializers.URLField(allow_null=True)
    totals=serializers.DictField(child=ReviewMoneyTotals())
    results=RecoverySaleHoldSummary(many=True)


def review_totals(rows):
    totals={state:{key:"0" for key in ("net_total_fcfa","payments_fcfa","unpaid_debt_fcfa","customer_credit_fcfa")}
        for state in ("pending","refused","validated")}
    debt=models.Case(models.When(net_total_fcfa__gt=models.F("payments_fcfa"),then=models.F("net_total_fcfa")-models.F("payments_fcfa")),
        default=0,output_field=models.BigIntegerField())
    credit=models.Case(models.When(payments_fcfa__gt=models.F("net_total_fcfa"),then=models.F("payments_fcfa")-models.F("net_total_fcfa")),
        default=0,output_field=models.BigIntegerField())
    for row in rows.order_by().values("state").annotate(net=StockTotal("net_total_fcfa"),paid=StockTotal("payments_fcfa"),
            debt=StockTotal(debt),credit=StockTotal(credit)):
        totals[row["state"]]=dict(zip(("net_total_fcfa","payments_fcfa","unpaid_debt_fcfa","customer_credit_fcfa"),
            (row["net"],row["paid"],row["debt"],row["credit"]),strict=True))
    return totals


class RecoverySaleHoldViewSet(viewsets.GenericViewSet):
    queryset=None
    serializer_class=RecoverySaleHoldReceipt
    parser_classes=[RecoveryJournalParser]
    pagination_class=RecoveryPagination

    def authorize(self):
        self.shop,_live=hold_context(self.kwargs["shop_id"],self.request.user,self.request.device_session.id)
        return self.shop

    def get_shop(self):return self.shop

    @extend_schema(request=RecoverySaleHoldInput,responses={200:RecoverySaleHoldReceipt,201:RecoverySaleHoldReceipt,**ERRORS},description=(
        "Current responsible marks a ready captured sale pending, or refuses an existing pending review with a reason. "
        "Use preview_token and review_version from integration-preview as previous_version. A source operation has one hold "
        "across copies and recovery generations. Pending quantities are reserved once per product, without validated sale, "
        "cash, debt or customer credit. Refusal retains the source and reservation until separate explicit regularization; "
        "it never releases stock or refunds money. An explicit reservation reconciliation can release only already accounted quantities. "
        "Same command/content returns its historical receipt after current authorization. "
        "Blocked source references and dependencies must be resolved before this adapter can stage a sale."))
    def decide(self,request,*args,**kwargs):
        self.authorize()
        receipt,created=decide_sale_hold(shop_id=self.shop.id,actor=request.user,session_id=request.device_session.id,
            capture_id=self.kwargs["pk"],data=request.data)
        return Response(receipt,status=201 if created else 200)

    @extend_schema(responses={200:RecoverySaleHoldReceipt(many=True),**ERRORS},description=(
        "Current responsible reads the protected, versioned decision history for this source operation after authorization "
        "and current-generation capture integrity verification. SQL pages: 25 default, 100 maximum; version/UUID order. "
        "Receipts are historical; the current state is in integration-preview and sales-review. Prior generations of this "
        "same shop/source can be inspected without exposing another shop or granting historical identity rights."))
    @transaction.atomic
    def history(self,request,*args,**kwargs):
        shop=self.authorize()
        hold=find_sale_hold(_capture(shop,self.kwargs["pk"]))
        rows=hold.decisions.all() if hold else RecoverySaleHoldDecision.objects.none()
        page=self.paginate_queryset(rows.only("id","version","receipt").order_by("version","id"))
        return self.get_paginated_response([row.receipt for row in page])

    @extend_schema(parameters=[RecoverySaleReviewQuery],responses={200:RecoverySaleHoldPage,**ERRORS},description=(
        "Current responsible reads this shop's recovered-sale review queue with optional state filter and SQL pagination "
        "(25 default, 100 maximum; creation/UUID order). Exact FCFA totals are decimal strings over the whole filter, "
        "separate by pending/refused/validated state. Declared payments are source facts; pending/refused money is excluded "
        "from validated cash/debt ledgers. Validated totals also remain original source facts, not current turnover, "
        "balances or refunds; read the business ledgers for current amounts. Holds survive generations. current_capture_id is null when no current copy has "
        "been deposited; origin_capture_id identifies the historical capture. No payload or other shop data is loaded per row."))
    @transaction.atomic
    def list(self,request,*args,**kwargs):
        shop=self.authorize()
        query=RecoverySaleReviewQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        rows=RecoverySaleHold.objects.filter(capture__shop=shop)
        if "state" in query.validated_data:rows=rows.filter(state=query.validated_data["state"])
        totals=review_totals(rows)
        current=RecoveryJournalOperation.objects.filter(shop=shop,session__generation__current_slot=1,
            operation_id=models.OuterRef("source_operation_id")).values("id")[:1]
        rows=rows.annotate(current_capture_id=models.Subquery(current)).select_related("capture").defer("capture__payload","capture__dependencies")
        page=self.paginate_queryset(rows.order_by("created_at","id"))
        response=self.get_paginated_response(RecoverySaleHoldSummary(page,many=True).data)
        response.data["totals"]=totals
        return response
