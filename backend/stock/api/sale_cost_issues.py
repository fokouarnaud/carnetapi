"""Manager inspection of sale lines whose latest captured cost is uncertain."""
from django.db import transaction
from django.db.models import CharField, Count, OuterRef, Prefetch, Subquery, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_field, extend_schema_serializer
from rest_framework import mixins, serializers, viewsets
from rest_framework.utils.urls import replace_query_param
from common.api.fields import StrictSerializer
from catalogue.models import Product
from sales.models import SaleLine
from stock.models import StockCostSnapshot
from stock.api.stock_cost_serializers import MovementCostSerializer
from purchasing.api.suppliers import supplier_shop


class SaleCostIssueSerializer(serializers.ModelSerializer):
    sale_id = serializers.UUIDField(read_only=True)
    product_id = serializers.UUIDField(read_only=True)
    occurred_at = serializers.DateTimeField(source="sale.occurred_at", read_only=True)
    base_quantity_milli = serializers.CharField(read_only=True)
    net_fcfa = serializers.CharField(read_only=True)
    cost_state = serializers.CharField(read_only=True)
    cost = serializers.SerializerMethodField()

    @extend_schema_field(MovementCostSerializer(allow_null=True))
    def get_cost(self, obj):
        movement = getattr(obj, "stock_movement", None)
        rows = movement.latest_costs if movement is not None else []
        return MovementCostSerializer(rows[0]).data if rows else None

    class Meta:
        model = SaleLine
        fields = ["id", "sale_id", "product_id", "occurred_at", "product_name", "base_quantity_milli", "net_fcfa", "cost_state", "cost"]
        read_only_fields = fields


class SaleCostIssueQuery(StrictSerializer):
    product_id = serializers.UUIDField(required=False)
    cost_state = serializers.ChoiceField(choices=["unknown", "provisional"], required=False)
    as_of = serializers.DateTimeField(required=False)
    page = serializers.IntegerField(min_value=1, required=False)
    page_size = serializers.IntegerField(min_value=1, required=False)

    def validate_as_of(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("future_cutoff_not_allowed", code="future_cutoff_not_allowed")
        return value


class SaleCostIssueTotals(serializers.Serializer):
    unknown_lines = serializers.IntegerField()
    provisional_lines = serializers.IntegerField()


@extend_schema_serializer(many=False)
class SaleCostIssuePage(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    as_of = serializers.DateTimeField()
    data_scope = serializers.CharField()
    totals = SaleCostIssueTotals()
    results = SaleCostIssueSerializer(many=True)


class SaleCostIssueViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = SaleCostIssueSerializer
    queryset = SaleLine.objects.none()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        shop = supplier_shop(self.kwargs["shop_id"], self.request)
        params = SaleCostIssueQuery(data=self.request.query_params)
        params.is_valid(raise_exception=True)
        values = params.validated_data
        self.cutoff = values.get("as_of", timezone.now())
        rows = SaleLine.objects.filter(product__shop=shop, sale__created_at__lte=self.cutoff, sale__occurred_at__lte=self.cutoff)
        if "product_id" in self.kwargs:
            rows = rows.filter(product=get_object_or_404(Product, id=self.kwargs["product_id"], shop=shop))
        if "product_id" in values:
            rows = rows.filter(product=get_object_or_404(Product, id=values["product_id"], shop=shop))
        latest_state = StockCostSnapshot.objects.filter(movement__sale_line_id=OuterRef("pk"), created_at__lte=self.cutoff).order_by("-version")
        latest_version = StockCostSnapshot.objects.filter(movement_id=OuterRef("movement_id"), created_at__lte=self.cutoff).order_by("-version")
        costs = StockCostSnapshot.objects.filter(created_at__lte=self.cutoff, version=Subquery(latest_version.values("version")[:1])).select_related("movement")
        rows = rows.annotate(
            cost_state=Coalesce(Subquery(latest_state.values("cost_state")[:1]), Value("unknown"), output_field=CharField())
        ).exclude(cost_state="known")
        if "cost_state" in values:
            rows = rows.filter(cost_state=values["cost_state"])
        return rows.select_related("sale", "stock_movement").prefetch_related(
            Prefetch("stock_movement__cost_snapshots", queryset=costs, to_attr="latest_costs")
        ).order_by("sale__occurred_at", "sale_id", "position", "id")

    @extend_schema(parameters=[SaleCostIssueQuery], responses=SaleCostIssuePage)
    @transaction.atomic
    def list(self, request, *args, **kwargs):
        rows = self.get_queryset()
        totals = {"unknown_lines": 0, "provisional_lines": 0}
        for group in rows.order_by().values("cost_state").annotate(total=Count("id")):
            totals[group["cost_state"] + "_lines"] = group["total"]
        response = self.get_paginated_response(self.get_serializer(self.paginate_queryset(rows), many=True).data)
        response.data.update(as_of=serializers.DateTimeField().to_representation(self.cutoff), data_scope="server_known", totals=totals)
        for name in ("next", "previous"):
            if response.data[name]:
                response.data[name] = replace_query_param(response.data[name], "as_of", self.cutoff.isoformat())
        return response
