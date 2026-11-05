"""Scoped collection review money remains distinct from validated ledgers."""
from django.db import models,transaction
from drf_spectacular.utils import extend_schema,extend_schema_serializer
from rest_framework import serializers,viewsets
from rest_framework.response import Response

from backups.api.recovery_integration import ERRORS
from backups.api.recovery_journals import RecoveryJournalParser
from backups.api.recovery_metadata import RecoveryPagination,RecoveryReportSerializer
from backups.models import RecoveryCollectionReview,RecoveryCollectionReviewDecision,RecoveryJournalOperation
from backups.services.recovery_collection_reviews import RecoveryCollectionReviewInput,collection_review_context,decide_collection_review,find_collection_review
from backups.services.recovery_integration import _capture,RecoveryTypeUnsupported
from stock.services.stock_queries import StockTotal


@extend_schema_serializer(many=False)
class RecoveryCollectionReviewReceipt(serializers.Serializer):
    id=serializers.UUIDField()
    review_id=serializers.UUIDField()
    capture_id=serializers.UUIDField()
    operation_id=serializers.UUIDField()
    source_operation_id=serializers.UUIDField()
    source_payment_id=serializers.UUIDField()
    customer_id=serializers.UUIDField()
    state=serializers.ChoiceField(choices=["pending","refused","validated"])
    version=serializers.IntegerField(min_value=1)
    reason=serializers.CharField()
    reviewed_by=serializers.UUIDField()
    reviewed_installation_id=serializers.UUIDField()
    reviewed_at=serializers.DateTimeField()
    integration_decision_id=serializers.UUIDField(allow_null=True)
    amount_fcfa=serializers.IntegerField(min_value=1)
    original_applied_fcfa=serializers.IntegerField(min_value=0)
    original_unallocated_fcfa=serializers.IntegerField(min_value=0)


class RecoveryCollectionReviewQuery(serializers.Serializer):
    state=serializers.ChoiceField(choices=["pending","refused","validated"],required=False)
    customer_id=serializers.UUIDField(required=False)


class RecoveryCollectionReviewSummary(serializers.ModelSerializer):
    source_payment_id=serializers.UUIDField(source="payment_id")
    origin_capture_id=serializers.UUIDField(source="capture_id")
    current_capture_id=serializers.UUIDField(allow_null=True)
    origin_user_id=serializers.UUIDField(source="capture.origin_user_id")
    origin_installation_id=serializers.UUIDField(source="capture.origin_installation_id")
    occurred_at=serializers.DateTimeField(source="capture.occurred_at")
    class Meta:
        model=RecoveryCollectionReview
        fields=["id","source_operation_id","source_payment_id","customer_id","origin_capture_id","current_capture_id",
            "origin_user_id","origin_installation_id","occurred_at","state","version","amount_fcfa",
            "original_applied_fcfa","original_unallocated_fcfa"]
        read_only_fields=fields


class CollectionReviewMoneyTotals(serializers.Serializer):
    amount_fcfa=serializers.CharField()
    original_applied_fcfa=serializers.CharField()
    original_unallocated_fcfa=serializers.CharField()


@extend_schema_serializer(many=False)
class RecoveryCollectionReviewPage(RecoveryReportSerializer):
    count=serializers.IntegerField()
    next=serializers.URLField(allow_null=True)
    previous=serializers.URLField(allow_null=True)
    totals=serializers.DictField(child=CollectionReviewMoneyTotals())
    results=RecoveryCollectionReviewSummary(many=True)


def collection_review_totals(rows):
    fields=("amount_fcfa","original_applied_fcfa","original_unallocated_fcfa")
    totals={state:dict.fromkeys(fields,"0") for state in ("pending","refused","validated")}
    for row in rows.order_by().values("state").annotate(**{name+"_sum":StockTotal(name) for name in fields}):
        totals[row["state"]]={name:row[name+"_sum"] for name in fields}
    return totals


class RecoveryCollectionReviewViewSet(viewsets.GenericViewSet):
    queryset=None
    serializer_class=RecoveryCollectionReviewReceipt
    pagination_class=RecoveryPagination
    parser_classes=[RecoveryJournalParser]

    def authorize(self):
        self.shop,_live=collection_review_context(self.kwargs["shop_id"],self.request.user,self.request.device_session.id)
        return self.shop

    def get_shop(self):return self.shop

    @extend_schema(request=RecoveryCollectionReviewInput,responses={200:RecoveryCollectionReviewReceipt,201:RecoveryCollectionReviewReceipt,**ERRORS},description=(
        "Current responsible marks a ready captured customer_collection pending, or refuses an existing pending review. "
        "Use review_version and preview_token from integration-preview and a distinct decision UUID with a reason. "
        "The one review survives source copies and recovery generations. Staging/refusal creates only review/audit records: "
        "no validated cash, debt allocation, credit, ledger, sale or stock effect and no rights for the source identity. "
        "Refusal preserves the event and money facts; separate explicit regularization remains required, without auto-refund "
        "or automatic reopening. Exact command replay rechecks current authority and returns the immutable receipt. "
        "Missing references/dependencies must be resolved before this ready-source adapter can stage the collection."))
    def decide(self,request,*args,**kwargs):
        self.authorize()
        receipt,created=decide_collection_review(shop_id=self.shop.id,actor=request.user,session_id=request.device_session.id,
            capture_id=self.kwargs["pk"],data=request.data)
        return Response(receipt,status=201 if created else 200)

    @extend_schema(responses={200:RecoveryCollectionReviewReceipt(many=True),**ERRORS},description=(
        "Current responsible reads this source collection's immutable review decisions in version/UUID order. "
        "Current generation capture integrity and shop authorization are checked first; historical decisions of the same "
        "review remain visible through a matching current copy. SQL pages default 25, maximum 100. "
        "A validated decision links the integration receipt and commits atomically with the business effects."))
    @transaction.atomic
    def history(self,request,*args,**kwargs):
        shop=self.authorize();capture=_capture(shop,self.kwargs["pk"])
        if capture.operation_type!="customer_collection":raise RecoveryTypeUnsupported()
        review=find_collection_review(capture)
        rows=review.decisions.all() if review else RecoveryCollectionReviewDecision.objects.none()
        page=self.paginate_queryset(rows.only("id","version","receipt").order_by("version","id"))
        return self.get_paginated_response([row.receipt for row in page])

    @extend_schema(parameters=[RecoveryCollectionReviewQuery],responses={200:RecoveryCollectionReviewPage,**ERRORS},description=(
        "Current responsible reads this shop's staged collection reviews across generations, optionally filtered by state/customer UUID. "
        "Authorization and filters precede stable creation/UUID SQL pagination (25 default, 100 maximum). "
        "Exact FCFA totals as decimal strings cover the entire filter, separated by pending/refused/validated state. "
        "Totals retain original source facts, including originally applied/unallocated amounts; validated review totals "
        "are not current customer balances or all shop collections. Read business ledgers for actual recovered allocations/refunds. "
        "Pending/refused money is excluded from those validated ledgers. current_capture_id is null until a matching "
        "current-generation copy is deposited. Original payloads/dependencies are not loaded per row. "
        "Raw captures never staged, blocked copies and directly integrated operations remain inspectable in their respective endpoints."))
    @transaction.atomic
    def list(self,request,*args,**kwargs):
        shop=self.authorize()
        query=RecoveryCollectionReviewQuery(data=request.query_params);query.is_valid(raise_exception=True)
        rows=RecoveryCollectionReview.objects.filter(capture__shop=shop)
        for key in ("state","customer_id"):
            if key in query.validated_data:rows=rows.filter(**{key:query.validated_data[key]})
        totals=collection_review_totals(rows)
        current=RecoveryJournalOperation.objects.filter(shop=shop,session__generation__current_slot=1,
            operation_id=models.OuterRef("source_operation_id")).values("id")[:1]
        rows=rows.annotate(current_capture_id=models.Subquery(current)).select_related("capture").defer("capture__payload","capture__dependencies")
        page=self.paginate_queryset(rows.order_by("created_at","id"))
        response=self.get_paginated_response(RecoveryCollectionReviewSummary(page,many=True).data)
        response.data["totals"]=totals
        return response
