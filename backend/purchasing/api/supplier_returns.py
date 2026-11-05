"""Confirmed supplier departures bounded by actual cumulative receipts."""
from stock.services.stock_alert_services import observe_stock
from django.db import transaction
from django.db.models import OuterRef, Prefetch, Subquery
from purchasing.services.supplier_ledger import append_supplier_ledger
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import mixins, serializers, viewsets
from rest_framework.response import Response
from common.services.operations import json_value, original_receipt
from common.services.errors import OperationConflict
from common.models import Activity
from purchasing.models import PurchaseDocument, PurchaseLine, SupplierReturn, SupplierReturnLine
from stock.models import StockCostSnapshot, StockMovement
from stock.services.movement_costs import freeze_operation_costs
from stock.api.stock_cost_serializers import MovementCostSerializer
from purchasing.api.purchases import ReceiptInput, context


class ReturnLineSerializer(serializers.ModelSerializer):
    line_id = serializers.UUIDField(read_only=True)
    cost = serializers.SerializerMethodField()

    @extend_schema_field(MovementCostSerializer(allow_null=True))
    def get_cost(self, obj):
        if hasattr(obj.movement, "latest_costs"):
            cost = obj.movement.latest_costs[0] if obj.movement.latest_costs else None
        else:
            cost = obj.movement.cost_snapshots.select_related("movement").order_by("-version").first()
        return MovementCostSerializer(cost).data if cost is not None else None

    class Meta:
        model = SupplierReturnLine
        fields = ["line_id", "quantity_milli", "base_quantity_milli", "cost"]
        read_only_fields = fields


class ReturnSerializer(serializers.ModelSerializer):
    document_id = serializers.UUIDField(read_only=True)
    lines = ReturnLineSerializer(many=True, read_only=True)
    actor_id = serializers.UUIDField(source="operation.actor_id", read_only=True)
    reason = serializers.CharField(source="operation.reason", read_only=True)

    class Meta:
        model = SupplierReturn
        fields = ["id", "document_id", "occurred_at", "created_at", "actor_id", "reason", "lines"]
        read_only_fields = fields


@transaction.atomic
def depart_supplier_return(shop_id, supplier_id, document_id, request, data):
    shop, supplier = context(shop_id, supplier_id, request)
    document = get_object_or_404(PurchaseDocument, id=document_id, shop=shop, supplier=supplier, kind="invoice")
    canonical = json_value({**data, "document_id": document.id, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "supplier_return_departed", canonical)
    if original is not None:
        return original, False
    if SupplierReturn.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    prepared = []
    for item in data["lines"]:
        line = get_object_or_404(PurchaseLine, id=item["line_id"], document=document)
        received = sum(part.quantity_milli for part in line.receipt_lines.all())
        returned = sum(part.quantity_milli for part in line.return_lines.all())
        quantity = item["quantity_milli"]
        if quantity > received - returned:
            raise serializers.ValidationError({"lines": "Le retour dépasse la quantité reçue non encore retournée."})
        scaled = quantity * line.factor_milli
        if scaled % 1000 or (line.product.unit_kind == "piece" and scaled % 1000000):
            raise serializers.ValidationError({"lines": "Quantité incompatible avec l'unité de base."})
        prepared.append((line, quantity, scaled // 1000))
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=request.user, action="supplier_return_departed",
                                    target_id=data["id"], reason=data["reason"], details={"request": canonical})
    departure = SupplierReturn.objects.create(id=data["id"], document=document, operation=event,
                                              installation=request.device_session.installation, occurred_at=data["occurred_at"])
    for line, quantity, base in prepared:
        movement = StockMovement.objects.create(product=line.product, actor=request.user, operation=event,
                                                 kind="supplier_return", quantity_milli=-base,
                                                 occurred_at=data["occurred_at"], reason=data["reason"])
        SupplierReturnLine.objects.create(supplier_return=departure, line=line, movement=movement,
                                           quantity_milli=quantity, base_quantity_milli=base)
    freeze_operation_costs(event, [line.product_id for line, _, _ in prepared], data["occurred_at"], kind="supplier_return")
    receipt = json_value(ReturnSerializer(departure).data)
    event.details["response"] = receipt
    event.save(update_fields=["details", "updated_at"])
    observe_stock([line.product_id for line, _, _ in prepared], event)
    append_supplier_ledger(shop, supplier, event, departure.id, "supplier_return", departure.occurred_at,
                           external_reference=document.external_reference)
    return receipt, True


class SupplierReturnViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = SupplierReturn.objects.none()
    serializer_class = ReturnSerializer

    @transaction.atomic
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        shop, supplier = context(self.kwargs["shop_id"], self.kwargs["supplier_id"], self.request)
        document = get_object_or_404(PurchaseDocument, id=self.kwargs["document_id"], shop=shop, supplier=supplier, kind="invoice")
        latest = StockCostSnapshot.objects.filter(movement_id=OuterRef("movement_id")).order_by("-version").values("version")[:1]
        costs = StockCostSnapshot.objects.filter(version=Subquery(latest)).select_related("movement")
        lines = SupplierReturnLine.objects.select_related("movement").prefetch_related(
            Prefetch("movement__cost_snapshots", queryset=costs, to_attr="latest_costs"))
        return SupplierReturn.objects.filter(document=document).select_related("operation").prefetch_related(
            Prefetch("lines", queryset=lines)).order_by("created_at", "id")

    @extend_schema(request=ReceiptInput, responses={200: ReturnSerializer, 201: ReturnSerializer})
    def create(self, request, *args, **kwargs):
        context(self.kwargs["shop_id"], self.kwargs["supplier_id"], request)
        serializer = ReceiptInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = depart_supplier_return(self.kwargs["shop_id"], self.kwargs["supplier_id"], self.kwargs["document_id"], request, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)
