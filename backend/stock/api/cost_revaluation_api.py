"""Current-session manager confirmation and paged immutable cost history."""
from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from common.api.fields import StrictSerializer
from common.services.operations import json_value, original_receipt
from stock.services.cost_revaluation import revalue_product_costs
from stock.services.cost_revaluation_preview import preview_cost_revaluation
from common.services.errors import OperationConflict
from common.models import Activity
from stock.models import CostRevaluation, CostRevaluationItem, ReturnCostRevision
from catalogue.models import Product
from stock.api.return_cost_serializers import ReturnCostSerializer, cost_representation
from stock.api.stock_cost_serializers import MovementCostSerializer
from purchasing.api.suppliers import supplier_shop


class PreviewChanged(APIException):
    status_code = 409
    default_code = "cost_revaluation_preview_changed"
    default_detail = {"code": "cost_revaluation_preview_changed"}


class CostTotalsSerializer(serializers.Serializer):
    total_nanos_fcfa = serializers.CharField(allow_null=True)
    unknown_lines = serializers.IntegerField()
    provisional_lines = serializers.IntegerField()


class CostPreviewSerializer(serializers.Serializer):
    preview_token = serializers.CharField()
    movement_changes = serializers.IntegerField()
    return_changes = serializers.IntegerField()
    reconstructed_movements = serializers.IntegerField()
    legacy_return_receipts = serializers.IntegerField()
    reconstruct_legacy_returns = serializers.BooleanField(default=False)
    reconstructed_return_receipts = serializers.IntegerField(default=0)
    sales_cost_before = CostTotalsSerializer()
    sales_cost_after = CostTotalsSerializer()
    loss_cost_before = CostTotalsSerializer(required=False)
    loss_cost_after = CostTotalsSerializer(required=False)
    valuation_requires_regularization = serializers.BooleanField(default=False)
    quantity_after_milli = serializers.CharField()
    stock_value_after_nanos_fcfa = serializers.CharField(allow_null=True)


class CostConfirmationInput(StrictSerializer):
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    preview_token = serializers.RegexField(r"\A[0-9a-f]{64}\Z")
    reconstruct_legacy_returns = serializers.BooleanField(default=False)


class CostPreviewQuery(StrictSerializer):
    reconstruct_legacy_returns = serializers.BooleanField(default=False)


class CostBatchSerializer(serializers.ModelSerializer):
    product_id = serializers.UUIDField(read_only=True)
    operation_id = serializers.UUIDField(read_only=True)
    actor_id = serializers.UUIDField(source="operation.actor_id", read_only=True)
    reason = serializers.CharField(source="operation.reason", read_only=True)
    summary = CostPreviewSerializer(source="operation.details.summary", read_only=True, allow_null=True)

    class Meta:
        model = CostRevaluation
        fields = ["id", "product_id", "operation_id", "actor_id", "reason", "created_at", "summary"]
        read_only_fields = fields


class CostItemSerializer(serializers.ModelSerializer):
    movement_id = serializers.UUIDField(read_only=True)
    previous = MovementCostSerializer(read_only=True, allow_null=True)
    revised = MovementCostSerializer(read_only=True)
    previous_legacy_unit_cost_micros = serializers.CharField(read_only=True, allow_null=True)

    class Meta:
        model = CostRevaluationItem
        fields = ["id", "movement_id", "previous", "revised", "previous_legacy_unit_cost_micros"]
        read_only_fields = fields


class ReturnRevisionHistorySerializer(serializers.ModelSerializer):
    receipt_id = serializers.UUIDField(source="origin.receipt_id", read_only=True)
    previous = serializers.SerializerMethodField()
    revised = serializers.SerializerMethodField()

    @extend_schema_field(ReturnCostSerializer)
    def get_previous(self, obj):
        return cost_representation(obj.supersedes if obj.supersedes_id else obj.origin)

    @extend_schema_field(ReturnCostSerializer)
    def get_revised(self, obj):
        return cost_representation(obj)

    class Meta:
        model = ReturnCostRevision
        fields = ["id", "receipt_id", "previous", "revised"]
        read_only_fields = fields


class CostRevaluationViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = CostRevaluation.objects.none()
    serializer_class = CostBatchSerializer

    def product(self):
        shop = supplier_shop(self.kwargs["shop_id"], self.request)
        return get_object_or_404(Product, id=self.kwargs["product_id"], shop=shop)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        return CostRevaluation.objects.filter(product=self.product()).select_related("operation").order_by("created_at", "id")

    @transaction.atomic
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @transaction.atomic
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(parameters=[CostPreviewQuery], responses=CostPreviewSerializer)
    @transaction.atomic
    def preview(self, request, *args, **kwargs):
        product = self.product()
        query = CostPreviewQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(CostPreviewSerializer(preview_cost_revaluation(product, **query.validated_data)).data)

    @extend_schema(request=CostConfirmationInput, responses={200: CostBatchSerializer, 201: CostBatchSerializer})
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        product = self.product()
        serializer = CostConfirmationInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        canonical_data = dict(data)
        if not canonical_data["reconstruct_legacy_returns"]:
            canonical_data.pop("reconstruct_legacy_returns")  # Preserve receipts created before this option existed.
        canonical = json_value({**canonical_data, "product_id": product.id, "installation_id": request.device_session.installation_id})
        original = original_receipt(data["operation_id"], product.shop, request.user, "cost_revalued", canonical)
        if original is not None:
            return Response(original, status=200)
        if CostRevaluation.objects.filter(id=data["id"]).exists():
            raise OperationConflict()
        summary = preview_cost_revaluation(product, reconstruct_legacy_returns=data["reconstruct_legacy_returns"])
        if summary["preview_token"] != data["preview_token"]:
            raise PreviewChanged()
        operation = Activity.objects.create(id=data["operation_id"], shop=product.shop, actor=request.user,
            action="cost_revalued", target_id=product.id, reason=data["reason"], details={"request": canonical, "summary": summary})
        batch = CostRevaluation.objects.create(id=data["id"], product=product, operation=operation,
            installation=request.device_session.installation)
        revalue_product_costs(batch, reconstruct_legacy_returns=data["reconstruct_legacy_returns"])
        operation.refresh_from_db()
        batch.operation = operation
        receipt = json_value(CostBatchSerializer(batch).data)
        operation.details = {**operation.details, "response": receipt}
        operation.save(update_fields=["details", "updated_at"])
        return Response(receipt, status=201)

    @extend_schema(responses=CostItemSerializer(many=True))
    @transaction.atomic
    def items(self, request, *args, **kwargs):
        batch = self.get_object()
        rows = batch.items.select_related("previous__movement", "revised__movement").order_by("movement__occurred_at", "id")
        return self.get_paginated_response(CostItemSerializer(self.paginate_queryset(rows), many=True).data)

    @extend_schema(responses=ReturnRevisionHistorySerializer(many=True))
    @transaction.atomic
    def returns(self, request, *args, **kwargs):
        batch = self.get_object()
        rows = batch.return_revisions.select_related("origin", "supersedes").order_by("created_at", "id")
        return self.get_paginated_response(ReturnRevisionHistorySerializer(self.paginate_queryset(rows), many=True).data)
