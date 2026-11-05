"""A current manager reviews physical shortages without approving historical costs."""
import hashlib
import json
from django.db import transaction
from django.db.models import OuterRef, Prefetch, Subquery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from common.api.fields import StrictSerializer
from common.services.operations import json_value, original_receipt
from common.services.errors import OperationConflict
from common.models import Activity
from catalogue.models import Product
from stock.models import StockCount, StockShortageReview, StockMovement, StockCostSnapshot
from stock.api.stock_cost_serializers import MovementCostSerializer
from stock.api.stock_valuations import current_valuation
from purchasing.api.suppliers import supplier_shop


class ShortageReviewUnavailable(serializers.ValidationError):
    default_detail = {"code": "nonnegative_stock_with_open_shortage_required"}


class ShortageReviewChanged(APIException):
    status_code = 409
    default_code = "stock_shortage_preview_changed"
    default_detail = {"code": "stock_shortage_preview_changed"}


class ShortagePreviewInput(StrictSerializer):
    count_id = serializers.UUIDField(allow_null=True, default=None)


class ShortageConfirmationInput(ShortagePreviewInput):
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    preview_token = serializers.RegexField(r"\A[0-9a-f]{64}\Z")
    reason = serializers.CharField(max_length=2000, allow_blank=False)


class ShortagePreviewSerializer(serializers.Serializer):
    preview_token = serializers.CharField()
    quantity_milli = serializers.CharField()
    movement_count = serializers.IntegerField()
    stock_value_nanos_fcfa = serializers.CharField(allow_null=True)
    count_id = serializers.UUIDField(allow_null=True)
    valuation_requires_regularization_after = serializers.BooleanField()
    historical_costs_unchanged = serializers.BooleanField()
    as_of = serializers.DateTimeField()


class ShortageReviewSerializer(serializers.ModelSerializer):
    product_id = serializers.UUIDField(read_only=True)
    operation_id = serializers.UUIDField(read_only=True)
    actor_id = serializers.UUIDField(source="operation.actor_id", read_only=True)
    reason = serializers.CharField(source="operation.reason", read_only=True)
    count_id = serializers.UUIDField(read_only=True, allow_null=True)
    stock_quantity_milli = serializers.CharField(read_only=True)
    stock_value_nanos_fcfa = serializers.CharField(read_only=True, allow_null=True)

    class Meta:
        model = StockShortageReview
        fields = ["id", "product_id", "operation_id", "actor_id", "reason", "occurred_at", "created_at",
            "count_id", "stock_quantity_milli", "stock_value_nanos_fcfa", "basis_fingerprint"]
        read_only_fields = fields


class ShortageMovementSerializer(serializers.ModelSerializer):
    operation_id = serializers.UUIDField(read_only=True)
    source_id = serializers.UUIDField(source="operation.target_id", read_only=True, allow_null=True)
    actor_id = serializers.UUIDField(source="operation.actor_id", read_only=True)
    recorded_at = serializers.DateTimeField(source="operation.created_at", read_only=True)
    quantity_milli = serializers.CharField(read_only=True)
    cost = serializers.SerializerMethodField()

    @extend_schema_field(MovementCostSerializer(allow_null=True))
    def get_cost(self, obj):
        if hasattr(obj, "latest_costs"):
            cost = obj.latest_costs[0] if obj.latest_costs else None
        else:
            cost = obj.cost_snapshots.select_related("movement").order_by("-version").first()
        return MovementCostSerializer(cost).data if cost is not None else None

    class Meta:
        model = StockMovement
        fields = ["id", "operation_id", "source_id", "actor_id", "kind", "quantity_milli", "occurred_at", "recorded_at", "reason", "cost"]
        read_only_fields = fields


def shortage_preview(product, count_id):
    count = None if count_id is None else get_object_or_404(StockCount, product=product, id=count_id)
    reading = current_valuation(product)
    state = reading.state
    if state.quantity_milli < 0 or not state.has_unresolved_shortage:
        raise ShortageReviewUnavailable()
    token = hashlib.sha256(json.dumps([str(product.id), reading.physical_fingerprint,
        None if count is None else str(count.id)], separators=(",", ":")).encode()).hexdigest()
    return {"preview_token": token, "quantity_milli": str(state.quantity_milli),
        "movement_count": product.stock_movements.filter(occurred_at__lte=reading.at).count(),
        "stock_value_nanos_fcfa": None if state.stock_value_nanos_fcfa is None else str(state.stock_value_nanos_fcfa),
        "count_id": None if count is None else str(count.id), "historical_costs_unchanged": True,
        "valuation_requires_regularization_after": state.has_unresolved_valuation or state.stock_value_nanos_fcfa is None
            or state.bucket_cost_is_provisional, "as_of": reading.at}, reading.physical_fingerprint


class StockShortageReviewViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = StockShortageReview.objects.none()
    serializer_class = ShortageReviewSerializer

    def product(self):
        shop = supplier_shop(self.kwargs["shop_id"], self.request)
        return get_object_or_404(Product, id=self.kwargs["product_id"], shop=shop)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        return StockShortageReview.objects.filter(product=self.product()).select_related("operation").order_by("occurred_at", "id")

    @transaction.atomic
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @transaction.atomic
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(responses=ShortageMovementSerializer(many=True))
    @transaction.atomic
    def movements(self, request, *args, **kwargs):
        product = self.product()
        latest = StockCostSnapshot.objects.filter(movement_id=OuterRef("movement_id")).order_by("-version").values("version")[:1]
        costs = StockCostSnapshot.objects.filter(version=Subquery(latest)).select_related("movement")
        rows = StockMovement.objects.filter(product=product, occurred_at__lte=timezone.now()).select_related("operation").prefetch_related(
            Prefetch("cost_snapshots", queryset=costs, to_attr="latest_costs")).order_by("occurred_at", "operation__created_at", "created_at", "id")
        return self.get_paginated_response(ShortageMovementSerializer(self.paginate_queryset(rows), many=True).data)

    @extend_schema(request=ShortagePreviewInput, responses=ShortagePreviewSerializer)
    @transaction.atomic
    def preview(self, request, *args, **kwargs):
        product = self.product()
        serializer = ShortagePreviewInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        summary, _basis = shortage_preview(product, serializer.validated_data["count_id"])
        return Response(ShortagePreviewSerializer(summary).data)

    @extend_schema(request=ShortageConfirmationInput, responses={200: ShortageReviewSerializer, 201: ShortageReviewSerializer})
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        product = self.product()
        serializer = ShortageConfirmationInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        canonical = json_value({**data, "product_id": product.id, "installation_id": request.device_session.installation_id})
        original = original_receipt(data["operation_id"], product.shop, request.user, "stock_shortage_reviewed", canonical)
        if original is not None:
            return Response(original, status=200)
        if StockShortageReview.objects.filter(id=data["id"]).exists():
            raise OperationConflict()
        try:
            summary, basis = shortage_preview(product, data["count_id"])
        except ShortageReviewUnavailable as error:
            raise ShortageReviewChanged() from error
        if summary["preview_token"] != data["preview_token"]:
            raise ShortageReviewChanged()
        operation = Activity.objects.create(id=data["operation_id"], shop=product.shop, actor=request.user,
            action="stock_shortage_reviewed", target_id=product.id, reason=data["reason"], details={"request": canonical})
        review = StockShortageReview.objects.create(id=data["id"], product=product, operation=operation,
            installation=request.device_session.installation, occurred_at=summary["as_of"], count_id=data["count_id"],
            stock_quantity_milli=int(summary["quantity_milli"]), stock_value_nanos_fcfa=None if
            summary["stock_value_nanos_fcfa"] is None else int(summary["stock_value_nanos_fcfa"]), basis_fingerprint=basis)
        receipt = json_value(ShortageReviewSerializer(review).data)
        operation.details["response"] = receipt
        operation.save(update_fields=["details", "updated_at"])
        return Response(receipt, status=201)
