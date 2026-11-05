"""Physical losses have exactly one stock effect and no automatic money effect."""
from stock.services.stock_alert_services import observe_stock
from django.db import transaction
from django.db.models import OuterRef, Prefetch, Subquery
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import mixins, serializers, viewsets
from rest_framework.response import Response
from identity.services.authentication import require_live_session
from common.api.fields import ExactIntegerField
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from common.services.errors import OperationConflict
from common.models import Activity
from catalogue.models import Product
from stock.models import StockCostSnapshot, StockLoss, StockMovement
from stock.services.movement_costs import freeze_physical_cost
from stock.api.stock_cost_serializers import MovementCostSerializer
from sales.services.pricing import MAX_FCFA
from purchasing.api.purchases import EventSerializer


class LossInput(EventSerializer):
    quantity_milli = ExactIntegerField(min_value=1, max_value=MAX_FCFA)


class LossSerializer(serializers.ModelSerializer):
    cost = serializers.SerializerMethodField()

    @extend_schema_field(MovementCostSerializer(allow_null=True))
    def get_cost(self, obj):
        if hasattr(obj.movement, "latest_costs"):
            cost = obj.movement.latest_costs[0] if obj.movement.latest_costs else None
        else:
            cost = obj.movement.cost_snapshots.select_related("movement").order_by("-version").first()
        return MovementCostSerializer(cost).data if cost is not None else None
    product_id = serializers.UUIDField(read_only=True)
    actor_id = serializers.UUIDField(source="operation.actor_id", read_only=True)
    reason = serializers.CharField(source="operation.reason", read_only=True)
    recorded_at = serializers.DateTimeField(source="operation.created_at", read_only=True)
    stock_after_milli = serializers.IntegerField(read_only=True)

    class Meta:
        model = StockLoss
        fields = ["id", "product_id", "actor_id", "reason", "recorded_at", "occurred_at", "quantity_milli",
                  "unit_kind", "unit_label", "stock_after_milli", "stock_requires_regularization", "cost"]
        read_only_fields = fields


def loss_context(shop_id, product_id, request):
    require_live_session(request.user, request.device_session.id, request.device_session.installation)
    shop = current_shop(shop_id, request.user, request.device_session.installation)
    require_manager(shop, request.user)
    return shop, get_object_or_404(Product, shop=shop, id=product_id)


@transaction.atomic
def record_loss(shop_id, product_id, request, data):
    shop, product = loss_context(shop_id, product_id, request)
    canonical = json_value({**data, "product_id": product.id, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "stock_loss_recorded", canonical)
    if original is not None:
        return original, False
    if StockLoss.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    quantity = data["quantity_milli"]
    if product.unit_kind == "piece" and quantity % 1000:
        raise serializers.ValidationError({"quantity_milli": "Une pièce doit avoir une quantité entière."})
    # Summation in Python preserves exact stock beyond a single SQLite int64.
    stock_after = sum(product.stock_movements.values_list("quantity_milli", flat=True).iterator(chunk_size=1000)) - quantity
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=request.user, action="stock_loss_recorded",
                                    target_id=data["id"], reason=data["reason"], details={"request": canonical})
    movement = StockMovement.objects.create(product=product, actor=request.user, operation=event, kind="loss",
                                             quantity_milli=-quantity, occurred_at=data["occurred_at"], reason=data["reason"])
    loss = StockLoss.objects.create(id=data["id"], product=product, operation=event, installation=request.device_session.installation,
                                    movement=movement, quantity_milli=quantity, unit_kind=product.unit_kind, unit_label=product.unit_label,
                                    occurred_at=data["occurred_at"], stock_after_milli=stock_after, stock_requires_regularization=stock_after < 0)
    freeze_physical_cost(movement)
    receipt = json_value(LossSerializer(loss).data)
    event.details["response"] = receipt
    event.save(update_fields=["details", "updated_at"])
    observe_stock([product.id], event)
    return receipt, True


class StockLossViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = StockLoss.objects.none()
    serializer_class = LossSerializer

    @transaction.atomic
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @transaction.atomic
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        _, product = loss_context(self.kwargs["shop_id"], self.kwargs["product_id"], self.request)
        latest = StockCostSnapshot.objects.filter(movement_id=OuterRef("movement_id")).order_by("-version").values("version")[:1]
        costs = StockCostSnapshot.objects.filter(version=Subquery(latest)).select_related("movement")
        return StockLoss.objects.filter(product=product).select_related("operation", "movement").prefetch_related(
            Prefetch("movement__cost_snapshots", queryset=costs, to_attr="latest_costs")).order_by("occurred_at", "id")

    @extend_schema(request=LossInput, responses={200: LossSerializer, 201: LossSerializer})
    def create(self, request, *args, **kwargs):
        loss_context(self.kwargs["shop_id"], self.kwargs["product_id"], request)
        serializer = LossInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = record_loss(self.kwargs["shop_id"], self.kwargs["product_id"], request, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)
