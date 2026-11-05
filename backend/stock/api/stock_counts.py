"""Fixed observations are compared against stock at the same effective instant."""
from stock.services.stock_alert_services import observe_stock
from django.db import transaction
from django.db.models import OuterRef, Prefetch, Subquery
from django.utils import timezone
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import mixins, serializers, viewsets
from rest_framework.response import Response
from rest_framework.exceptions import APIException
from common.api.fields import ExactIntegerField, StrictSerializer
from common.services.operations import json_value, original_receipt
from common.services.errors import OperationConflict
from common.models import Activity
from stock.models import StockCount, StockCountAdjustment, StockCostSnapshot, StockMovement
from stock.services.movement_costs import freeze_physical_cost
from stock.api.stock_cost_serializers import MovementCostSerializer
from sales.services.pricing import MAX_FCFA
from stock.api.stock_losses import loss_context


def expected_stock_at(product, counted_at, count=None):
    rows = product.stock_movements.filter(occurred_at__lte=counted_at)
    if count is not None:
        rows = rows.exclude(count_adjustment__count=count)
    return sum(rows.values_list("quantity_milli", flat=True).iterator(chunk_size=1000))


def applied_adjustment(count):
    return sum(count.adjustments.values_list("delta_milli", flat=True).iterator(chunk_size=1000))


class CountChanged(APIException):
    status_code = 409
    default_code = "stock_count_changed"
    default_detail = "Le rapprochement a changé. Relire puis confirmer l'écart actuel."


class ConfirmationInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    expected_milli = ExactIntegerField()
    applied_adjustment_milli = ExactIntegerField()
    reason = serializers.CharField(max_length=2000, allow_blank=False)


class AdjustmentSerializer(serializers.ModelSerializer):
    cost = serializers.SerializerMethodField()

    @extend_schema_field(MovementCostSerializer(allow_null=True))
    def get_cost(self, obj):
        if obj.movement_id is None:
            return None
        if hasattr(obj.movement, "latest_costs"):
            cost = obj.movement.latest_costs[0] if obj.movement.latest_costs else None
        else:
            cost = obj.movement.cost_snapshots.select_related("movement").order_by("-version").first()
        return MovementCostSerializer(cost).data if cost is not None else None

    count_id = serializers.UUIDField(read_only=True)
    expected_milli = serializers.IntegerField(read_only=True)
    after_applied_adjustment_milli = serializers.IntegerField(source="target_adjustment_milli", read_only=True)
    actor_id = serializers.UUIDField(source="operation.actor_id", read_only=True)
    reason = serializers.CharField(source="operation.reason", read_only=True)

    class Meta:
        model = StockCountAdjustment
        fields = ["id", "count_id", "actor_id", "reason", "created_at", "expected_milli", "delta_milli", "after_applied_adjustment_milli", "cost"]
        read_only_fields = fields


class CountHistorySerializer(serializers.ModelSerializer):
    actor_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Activity
        fields = ["id", "actor_id", "action", "reason", "details", "created_at"]
        read_only_fields = fields


class CountInput(StrictSerializer):
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    counted_at = serializers.DateTimeField()
    physical_milli = ExactIntegerField(min_value=0, max_value=MAX_FCFA)
    reason = serializers.CharField(max_length=2000, default="", allow_blank=True)

    def validate_counted_at(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("Un comptage physique ne peut pas être daté dans le futur.")
        return value


class StockCountSerializer(serializers.ModelSerializer):
    product_id = serializers.UUIDField(read_only=True)
    actor_id = serializers.UUIDField(source="operation.actor_id", read_only=True)
    recorded_at = serializers.DateTimeField(source="operation.created_at", read_only=True)
    expected_at_creation_milli = serializers.IntegerField(read_only=True)
    expected_milli = serializers.IntegerField(source="current_expected_milli", read_only=True)
    difference_milli = serializers.IntegerField(source="current_difference_milli", read_only=True)
    expected_revised = serializers.BooleanField(source="current_expected_revised", read_only=True)
    provisional = serializers.BooleanField(default=True, read_only=True)
    applied_adjustment_milli = serializers.IntegerField(source="current_applied_milli", read_only=True)
    proposed_adjustment_milli = serializers.IntegerField(source="current_proposed_milli", read_only=True)

    def to_representation(self, instance):
        expected = expected_stock_at(instance.product, instance.counted_at, instance)
        instance.current_expected_milli = expected
        instance.current_difference_milli = instance.physical_milli - expected
        instance.current_expected_revised = expected != instance.expected_at_creation_milli
        instance.current_applied_milli = applied_adjustment(instance)
        instance.current_proposed_milli = instance.current_difference_milli - instance.current_applied_milli
        return super().to_representation(instance)

    class Meta:
        model = StockCount
        fields = ["id", "product_id", "actor_id", "recorded_at", "counted_at", "physical_milli", "unit_kind", "unit_label",
                  "expected_at_creation_milli", "expected_milli", "difference_milli", "expected_revised", "provisional",
                  "applied_adjustment_milli", "proposed_adjustment_milli"]
        read_only_fields = fields


@transaction.atomic
def record_count(shop_id, product_id, request, data):
    shop, product = loss_context(shop_id, product_id, request)
    canonical = json_value({**data, "product_id": product.id, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "stock_count_recorded", canonical)
    if original is not None:
        return original, False
    if StockCount.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    if product.unit_kind == "piece" and data["physical_milli"] % 1000:
        raise serializers.ValidationError({"physical_milli": "Une pièce doit avoir une quantité entière."})
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=request.user, action="stock_count_recorded",
                                    target_id=data["id"], reason=data["reason"], details={"request": canonical})
    count = StockCount.objects.create(id=data["id"], product=product, operation=event, installation=request.device_session.installation,
                                      physical_milli=data["physical_milli"], counted_at=data["counted_at"], unit_kind=product.unit_kind,
                                      unit_label=product.unit_label, expected_at_creation_milli=expected_stock_at(product, data["counted_at"]))
    receipt = json_value(StockCountSerializer(count).data)
    event.details["response"] = receipt
    event.save(update_fields=["details", "updated_at"])
    return receipt, True


@transaction.atomic
def confirm_count(shop_id, product_id, count_id, request, data):
    shop, product = loss_context(shop_id, product_id, request)
    count = get_object_or_404(StockCount, id=count_id, product=product)
    canonical = json_value({**data, "count_id": count.id, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "stock_count_adjusted", canonical)
    if original is not None:
        return original, False
    expected = expected_stock_at(product, count.counted_at, count)
    applied = applied_adjustment(count)
    if expected != data["expected_milli"] or applied != data["applied_adjustment_milli"]:
        raise CountChanged()
    target = count.physical_milli - expected
    delta = target - applied
    if not -MAX_FCFA <= delta <= MAX_FCFA:
        raise serializers.ValidationError("Quantité de correction hors capacité d'un mouvement.")
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=request.user, action="stock_count_adjusted",
                                    target_id=count.id, reason=data["reason"], details={"request": canonical, "before_applied_milli": applied})
    movement = None
    if delta:
        movement = StockMovement.objects.create(product=product, actor=request.user, operation=event, kind="inventory_adjustment",
                                                 quantity_milli=delta, occurred_at=count.counted_at, reason=data["reason"])
    adjustment = StockCountAdjustment.objects.create(count=count, operation=event, installation=request.device_session.installation,
                                                      movement=movement, expected_milli=expected, target_adjustment_milli=target, delta_milli=delta)
    if movement is not None:
        freeze_physical_cost(movement)
    receipt = json_value(AdjustmentSerializer(adjustment).data)
    event.details["response"] = receipt
    event.save(update_fields=["details", "updated_at"])
    observe_stock([product.id], event)
    return receipt, True


class StockCountViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = StockCount.objects.none()
    serializer_class = StockCountSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        _, product = loss_context(self.kwargs["shop_id"], self.kwargs["product_id"], self.request)
        return StockCount.objects.filter(product=product).select_related("operation", "product").order_by("counted_at", "id")

    @extend_schema(request=CountInput, responses={200: StockCountSerializer, 201: StockCountSerializer})
    def create(self, request, *args, **kwargs):
        loss_context(self.kwargs["shop_id"], self.kwargs["product_id"], request)
        serializer = CountInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = record_count(self.kwargs["shop_id"], self.kwargs["product_id"], request, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)

    @extend_schema(request=ConfirmationInput, responses={200: AdjustmentSerializer, 201: AdjustmentSerializer})
    def confirm(self, request, *args, **kwargs):
        loss_context(self.kwargs["shop_id"], self.kwargs["product_id"], request)
        serializer = ConfirmationInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = confirm_count(self.kwargs["shop_id"], self.kwargs["product_id"], self.kwargs["pk"], request, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)

    @extend_schema(responses=AdjustmentSerializer(many=True))
    @transaction.atomic
    def adjustments(self, request, *args, **kwargs):
        count = self.get_object()
        latest = StockCostSnapshot.objects.filter(movement_id=OuterRef("movement_id")).order_by("-version").values("version")[:1]
        costs = StockCostSnapshot.objects.filter(version=Subquery(latest)).select_related("movement")
        rows = count.adjustments.select_related("operation", "movement").prefetch_related(
            Prefetch("movement__cost_snapshots", queryset=costs, to_attr="latest_costs")).order_by("created_at", "id")
        page = self.paginate_queryset(rows)
        return self.get_paginated_response(AdjustmentSerializer(page, many=True).data)

    @extend_schema(responses=CountHistorySerializer(many=True))
    @transaction.atomic
    def history(self, request, *args, **kwargs):
        count = self.get_object()
        events = Activity.objects.filter(shop_id=count.product.shop_id, target_id=count.id,
                                         action__in=["stock_count_recorded", "stock_count_adjusted"]).order_by("created_at", "id")
        page = self.paginate_queryset(events)
        return self.get_paginated_response(CountHistorySerializer(page, many=True).data)
