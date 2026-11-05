"""Shop-scoped, SQL-paged physical returns still awaiting a manager decision."""
from django.db import transaction
from django.db.models import BooleanField, CharField, F, Func, Value
from drf_spectacular.utils import extend_schema, extend_schema_field
from stock.api.return_cost_serializers import ReturnCostSerializer, cost_representation, ReceiptCostFields
from stock.services.return_costs import current_return_cost
from rest_framework import mixins, serializers, viewsets
from identity.services.authentication import require_live_session
from common.api.fields import StrictSerializer
from common.services.operations import current_shop, require_manager
from stock.models import ReturnReceipt
from stock.services.unsellable_balance import remaining_unsellable, with_unsellable_balance


class NonSellableStockQuery(StrictSerializer):
    product_id = serializers.UUIDField(required=False)
    include_closed = serializers.BooleanField(default=False)
    page = serializers.IntegerField(min_value=1, required=False)
    page_size = serializers.IntegerField(min_value=1, required=False)


class NonSellableStockSerializer(ReceiptCostFields):
    cost = serializers.SerializerMethodField()

    @extend_schema_field(ReturnCostSerializer(allow_null=True))
    def get_cost(self, obj):
        return cost_representation(current_return_cost(obj))
    id = serializers.UUIDField()
    product_id = serializers.UUIDField(source="return_line.sale_line.product_id")
    product_name = serializers.CharField(source="return_line.sale_line.product_name")
    sale_id = serializers.UUIDField(source="return_line.sale_line.sale_id")
    sale_return_id = serializers.UUIDField(source="return_line.sale_return_id")
    return_line_id = serializers.UUIDField()
    unit_kind = serializers.CharField(source="return_line.unit_kind")
    quantity_milli = serializers.IntegerField()
    remaining_unsellable_milli = serializers.SerializerMethodField()
    occurred_at = serializers.DateTimeField()

    def get_remaining_unsellable_milli(self, obj) -> int:
        return remaining_unsellable(obj)


class NonSellableStockViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = ReturnReceipt.objects.none()
    serializer_class = NonSellableStockSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        request = self.request
        require_live_session(request.user, request.device_session.id, request.device_session.installation)
        shop = current_shop(self.kwargs["shop_id"], request.user, request.device_session.installation)
        require_manager(shop, request.user)
        query = NonSellableStockQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        rows = ReturnReceipt.objects.filter(return_line__sale_return__sale__shop=shop, condition="unsellable")
        if query.validated_data.get("product_id"):
            rows = rows.filter(return_line__sale_line__product_id=query.validated_data["product_id"])
        rows = with_unsellable_balance(rows)
        if not query.validated_data["include_closed"]:
            rows = rows.annotate(remaining_quantity_text=Func(F("quantity_milli"), F("decided_quantity_milli"),
                function="stock_integer_subtract", output_field=CharField())).annotate(is_closed=Func(
                F("remaining_quantity_text"), Value(0), function="stock_at_threshold", output_field=BooleanField())).filter(is_closed=False)
        return rows.select_related("return_line", "return_line__sale_line", "cost_snapshot").order_by("occurred_at", "id")

    @extend_schema(parameters=[NonSellableStockQuery], responses=NonSellableStockSerializer(many=True))
    @transaction.atomic
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)
