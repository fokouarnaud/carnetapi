"""Explicit, preview-bound confirmation of the value of remaining physical stock."""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from common.api.fields import ExactIntegerField, StrictSerializer
from common.services.operations import json_value, original_receipt
from common.services.errors import OperationConflict
from common.models import Activity
from catalogue.models import Product
from stock.models import StockValuationAdjustment
from stock.services.movement_costs import iter_movement_valuations, ordered_product_cost_events
from stock.services.moving_average_cost import AverageCostState
from sales.services.pricing import MAX_FCFA
from purchasing.api.suppliers import supplier_shop


class ValuationChanged(APIException):
    status_code = 409
    default_code = "stock_valuation_preview_changed"
    default_detail = {"code": "stock_valuation_preview_changed"}


class ValuationUnavailable(serializers.ValidationError):
    pass


class PositiveStockRequired(ValuationUnavailable):
    default_detail = {"code": "positive_remaining_stock_required"}


class EmptyStockValueRequired(ValuationUnavailable):
    default_detail = {"code": "empty_stock_value_must_be_zero"}


class ValuationGapRequired(ValuationUnavailable):
    default_detail = {"code": "open_valuation_gap_required"}


@dataclass(frozen=True)
class StockValuationReading:
    state: AverageCostState
    basis_fingerprint: str
    physical_fingerprint: str
    invalid_adjustments_count: int
    invalid_shortage_reviews_count: int
    at: datetime


def current_valuation(product, *, at=None):
    at = at or timezone.now()
    digest = hashlib.sha256()
    physical = hashlib.sha256()
    state = AverageCostState()
    invalid = 0
    invalid_reviews = 0
    for event, state, _cost in iter_movement_valuations(
            ordered_product_cost_events(product.id, until=at), history_digest=digest, physical_digest=physical):
        if event.kind == "valuation_adjustment" and not event.basis_matches:
            invalid += 1
        if event.kind == "shortage_review" and not event.basis_matches:
            invalid_reviews += 1
    return StockValuationReading(state, digest.hexdigest(), physical.hexdigest(), invalid, invalid_reviews, at)


def valuation_preview(product, value):
    reading = current_valuation(product)
    state, basis, invalid, at = reading.state, reading.basis_fingerprint, reading.invalid_adjustments_count, reading.at
    if state.quantity_milli < 0:
        raise PositiveStockRequired()
    if state.quantity_milli == 0:
        if value != 0:
            raise EmptyStockValueRequired()
        if not state.has_unresolved_valuation:
            raise ValuationGapRequired()
    token = hashlib.sha256(json.dumps([str(product.id), basis, value], separators=(",", ":")).encode()).hexdigest()
    return {"preview_token": token, "quantity_milli": str(state.quantity_milli),
        "valuation_action": "close_empty_gap" if state.quantity_milli == 0 else "set_remaining_value",
        "previous_value_nanos_fcfa": None if state.stock_value_nanos_fcfa is None else str(state.stock_value_nanos_fcfa),
        "value_nanos_fcfa": str(value * 1000000000), "invalid_adjustments_count": invalid,
        "valuation_requires_regularization_after": state.has_unresolved_shortage, "as_of": at}, basis


class ValuationInput(StrictSerializer):
    value_fcfa = ExactIntegerField(min_value=0, max_value=MAX_FCFA)


class ValuationConfirmation(ValuationInput):
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    preview_token = serializers.RegexField(r"\A[0-9a-f]{64}\Z")
    reason = serializers.CharField(max_length=2000, allow_blank=False)


class ValuationPreviewSerializer(serializers.Serializer):
    preview_token = serializers.CharField()
    valuation_action = serializers.ChoiceField(choices=["set_remaining_value", "close_empty_gap"])
    quantity_milli = serializers.CharField()
    previous_value_nanos_fcfa = serializers.CharField(allow_null=True)
    value_nanos_fcfa = serializers.CharField()
    invalid_adjustments_count = serializers.IntegerField()
    valuation_requires_regularization_after = serializers.BooleanField()
    as_of = serializers.DateTimeField()


class CurrentValuationSerializer(serializers.Serializer):
    quantity_milli = serializers.CharField()
    stock_value_nanos_fcfa = serializers.CharField(allow_null=True)
    stock_value_state = serializers.ChoiceField(choices=["known", "provisional", "unknown"])
    has_unresolved_valuation = serializers.BooleanField()
    has_unresolved_shortage = serializers.BooleanField()
    valuation_requires_regularization = serializers.BooleanField()
    invalid_adjustments_count = serializers.IntegerField()
    invalid_shortage_reviews_count = serializers.IntegerField()
    as_of = serializers.DateTimeField()


class ValuationSerializer(serializers.ModelSerializer):
    valuation_action = serializers.SerializerMethodField()

    @extend_schema_field(serializers.ChoiceField(choices=["set_remaining_value", "close_empty_gap"]))
    def get_valuation_action(self, obj):
        return "close_empty_gap" if obj.stock_quantity_milli == 0 else "set_remaining_value"

    product_id = serializers.UUIDField(read_only=True)
    operation_id = serializers.UUIDField(read_only=True)
    actor_id = serializers.UUIDField(source="operation.actor_id", read_only=True)
    reason = serializers.CharField(source="operation.reason", read_only=True)
    stock_quantity_milli = serializers.CharField(read_only=True)
    previous_value_nanos_fcfa = serializers.CharField(read_only=True, allow_null=True)
    value_fcfa = serializers.CharField(read_only=True)

    class Meta:
        model = StockValuationAdjustment
        fields = ["id", "product_id", "operation_id", "actor_id", "reason", "occurred_at", "created_at",
            "stock_quantity_milli", "previous_value_nanos_fcfa", "value_fcfa", "basis_fingerprint", "valuation_action"]
        read_only_fields = fields


class StockValuationViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = StockValuationAdjustment.objects.none()
    serializer_class = ValuationSerializer

    def product(self):
        shop = supplier_shop(self.kwargs["shop_id"], self.request)
        return get_object_or_404(Product, id=self.kwargs["product_id"], shop=shop)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        return StockValuationAdjustment.objects.filter(product=self.product()).select_related("operation").order_by("occurred_at", "id")

    @transaction.atomic
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @transaction.atomic
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(responses=CurrentValuationSerializer)
    @transaction.atomic
    def current(self, request, *args, **kwargs):
        reading = current_valuation(self.product())
        state, invalid, at = reading.state, reading.invalid_adjustments_count, reading.at
        return Response(CurrentValuationSerializer({"quantity_milli": str(state.quantity_milli),
            "stock_value_nanos_fcfa": None if state.stock_value_nanos_fcfa is None else str(state.stock_value_nanos_fcfa),
            "stock_value_state": "unknown" if state.stock_value_nanos_fcfa is None else
                "provisional" if state.quantity_milli > 0 and state.bucket_cost_is_provisional else "known",
            "has_unresolved_valuation": state.has_unresolved_valuation,
            "has_unresolved_shortage": state.has_unresolved_shortage,
            "valuation_requires_regularization": state.has_unresolved_valuation or state.has_unresolved_shortage
                or state.stock_value_nanos_fcfa is None or state.bucket_cost_is_provisional,
            "invalid_adjustments_count": invalid, "invalid_shortage_reviews_count": reading.invalid_shortage_reviews_count,
            "as_of": at}).data)

    @extend_schema(request=ValuationInput, responses=ValuationPreviewSerializer)
    @transaction.atomic
    def preview(self, request, *args, **kwargs):
        product = self.product()
        serializer = ValuationInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        summary, _basis = valuation_preview(product, serializer.validated_data["value_fcfa"])
        return Response(ValuationPreviewSerializer(summary).data)

    @extend_schema(request=ValuationConfirmation, responses={200: ValuationSerializer, 201: ValuationSerializer})
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        product = self.product()
        serializer = ValuationConfirmation(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        canonical = json_value({**data, "product_id": product.id, "installation_id": request.device_session.installation_id})
        original = original_receipt(data["operation_id"], product.shop, request.user, "stock_valuation_confirmed", canonical)
        if original is not None:
            return Response(original, status=200)
        if StockValuationAdjustment.objects.filter(id=data["id"]).exists():
            raise OperationConflict()
        try:
            summary, basis = valuation_preview(product, data["value_fcfa"])
        except ValuationUnavailable as error:
            raise ValuationChanged() from error
        if summary["preview_token"] != data["preview_token"]:
            raise ValuationChanged()
        operation = Activity.objects.create(id=data["operation_id"], shop=product.shop, actor=request.user,
            action="stock_valuation_confirmed", target_id=product.id, reason=data["reason"], details={"request": canonical})
        decision = StockValuationAdjustment.objects.create(id=data["id"], product=product, operation=operation,
            installation=request.device_session.installation, occurred_at=summary["as_of"],
            stock_quantity_milli=int(summary["quantity_milli"]), previous_value_nanos_fcfa=None if
            summary["previous_value_nanos_fcfa"] is None else int(summary["previous_value_nanos_fcfa"]),
            value_fcfa=data["value_fcfa"], basis_fingerprint=basis)
        receipt = json_value(ValuationSerializer(decision).data)
        operation.details["response"] = receipt
        operation.save(update_fields=["details", "updated_at"])
        return Response(receipt, status=201)
