"""Confirm actual returned goods without equating a financial return to restocking."""
import uuid
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from common.api.fields import ExactIntegerField
from common.services.operations import json_value, original_receipt
from common.services.errors import OperationConflict
from common.models import Activity
from common.services.origins import with_public_origin
from common.api.errors import OriginReferenceError
from sales.models import SaleReturnLine
from stock.models import ReturnReceipt, ReturnCostSnapshot, StockMovement
from stock.services.return_costs import receipt_cost_inputs, integral_unit_cost, current_return_cost
from stock.api.return_cost_serializers import ReturnCostSerializer, cost_representation, ReceiptCostFields
from sales.services.pricing import MAX_FCFA
from purchasing.api.purchases import EventSerializer
from sales.api.sale_returns import return_context
from stock.services.stock_alert_services import observe_stock
from stock.services.unsellable_balance import remaining_unsellable, with_unsellable_balance


class ReceiptQuantityConflict(APIException):
    status_code = 409
    default_code = "return_receipt_quantity_exceeded"
    default_detail = _("La quantité reçue dépasse le retour validé.")


class ReturnReceiptInput(EventSerializer):
    quantity_milli = ExactIntegerField(min_value=1, max_value=MAX_FCFA)
    condition = serializers.ChoiceField(choices=ReturnReceipt._meta.get_field("condition").choices)


class ReturnReceiptSerializer(ReceiptCostFields, serializers.ModelSerializer):
    cost = serializers.SerializerMethodField()

    @extend_schema_field(ReturnCostSerializer(allow_null=True))
    def get_cost(self, obj):
        return cost_representation(current_return_cost(obj))
    remaining_unsellable_milli = serializers.SerializerMethodField()

    def get_remaining_unsellable_milli(self, obj) -> int:
        return remaining_unsellable(obj)

    return_line_id = serializers.UUIDField(read_only=True)
    movement_id = serializers.UUIDField(read_only=True, allow_null=True)
    actor_id = serializers.SerializerMethodField()
    reason = serializers.CharField(source="operation.reason", read_only=True)

    @extend_schema_field(serializers.UUIDField())
    def get_actor_id(self,obj):
        return str(self.context.get('origin_identity',{}).get('actor_id',getattr(obj,'public_actor_id',obj.operation.actor_id)))

    class Meta:
        model = ReturnReceipt
        fields = ["id", "return_line_id", "actor_id", "reason", "occurred_at", "created_at", "quantity_milli", "condition",
                  "movement_id", "unit_cost_micros", "cost_is_provisional", "remaining_unsellable_milli", "cost"]
        read_only_fields = fields


def receipt_context(kwargs, request):
    shop, sale = return_context(kwargs["shop_id"], kwargs["sale_id"], request)
    line = get_object_or_404(SaleReturnLine.objects.select_related("sale_line"), id=kwargs["line_id"],
        sale_return_id=kwargs["return_id"], sale_return__sale=sale)
    return shop, sale, line


@transaction.atomic
def record_receipt(kwargs, request, data):
    shop, sale, line = receipt_context(kwargs, request)
    canonical = json_value({**data, "return_line_id": line.id, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "return_received", canonical)
    if original is not None:
        return original, False
    if ReturnReceipt.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    quantity = data["quantity_milli"]
    if line.unit_kind == "piece" and quantity % 1000:
        raise serializers.ValidationError({"quantity_milli": _("Une pièce doit avoir une quantité entière.")})
    before = sum(line.receipts.values_list("quantity_milli", flat=True).iterator(chunk_size=1000))
    if before + quantity > line.quantity_milli:
        raise ReceiptQuantityConflict()
    if data["occurred_at"] < sale.occurred_at:
        raise serializers.ValidationError({"occurred_at": _("La réception ne peut pas précéder la vente.")})
    cost_values = receipt_cost_inputs(line, quantity)
    return _persist_return_receipt(shop=shop,line=line,actor=request.user,installation=request.device_session.installation,
        data=data,canonical=canonical,cost_values=cost_values)


def _persist_return_receipt(*,shop,line,actor,installation,data,canonical,cost_values,
        origin_capture=None,source_snapshot=None,recovery_review=None):
    """Write checked physical quantities and costs under the caller's transaction."""
    operation = Activity.objects.create(id=data['operation_id'],shop=shop,actor=actor,
        action='return_received',target_id=data['id'],reason=data['reason'],details={'request':canonical})
    context={}
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=operation,capture=origin_capture)
        context['origin_identity']={'actor_id':origin_capture.origin_user_id}
        operation.details['recovery']=json_value({'capture_id':origin_capture.id,'origin_user_id':origin_capture.origin_user_id,
            'origin_installation_id':origin_capture.origin_installation_id,'origin_recorded_at':origin_capture.origin_recorded_at,
            'origin_installation_verified':False,'source_snapshot':source_snapshot,'review':recovery_review})
    quantity=data['quantity_milli'];movement=None
    cost = integral_unit_cost(cost_values) if cost_values is not None else source_snapshot['unit_cost_micros']
    provisional=cost_values['cost_state']!='known' if cost_values is not None else source_snapshot['cost_is_provisional']
    if data["condition"] == "sellable":
        movement = StockMovement.objects.create(id=uuid.uuid5(data["id"], "return-stock"), product_id=line.sale_line.product_id,
            actor=actor, operation=operation, kind="customer_return", quantity_milli=quantity, unit_cost_micros=cost,
            occurred_at=data["occurred_at"], reason=data["reason"])
    received = ReturnReceipt.objects.create(id=data["id"], return_line=line, operation=operation,
        installation=installation, quantity_milli=quantity, condition=data["condition"],
        movement=movement, unit_cost_micros=cost, cost_is_provisional=provisional,
        occurred_at=data["occurred_at"])
    if cost_values is not None:
        cost_identity={'id':source_snapshot['cost']['receipt_cost_id']} if source_snapshot is not None else {}
        ReturnCostSnapshot.objects.create(receipt=received,**cost_identity,**cost_values)
    receipt = json_value(ReturnReceiptSerializer(received,context=context).data)
    operation.details["response"] = receipt
    operation.save(update_fields=["details", "updated_at"])
    if movement is not None:
        observe_stock([line.sale_line.product_id], operation)
    return receipt, True


class ReturnReceiptViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = ReturnReceipt.objects.none()
    serializer_class = ReturnReceiptSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        _, _, line = receipt_context(self.kwargs, self.request)
        rows=with_unsellable_balance(ReturnReceipt.objects.filter(return_line=line)).annotate(shop_id=F('return_line__sale_return__sale__shop_id'))
        return with_public_origin(rows,actor_path='operation__actor_id').select_related("operation", "cost_snapshot").order_by("occurred_at", "id")

    @extend_schema(responses={200:ReturnReceiptSerializer(many=True),409:OriginReferenceError})
    @transaction.atomic
    def list(self,request,*args,**kwargs):return super().list(request,*args,**kwargs)

    @extend_schema(responses={200:ReturnReceiptSerializer,409:OriginReferenceError})
    @transaction.atomic
    def retrieve(self,request,*args,**kwargs):return super().retrieve(request,*args,**kwargs)

    @extend_schema(request=ReturnReceiptInput, responses={200: ReturnReceiptSerializer, 201: ReturnReceiptSerializer})
    def create(self, request, *args, **kwargs):
        receipt_context(self.kwargs, request)
        serializer = ReturnReceiptInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = record_receipt(self.kwargs, request, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)
