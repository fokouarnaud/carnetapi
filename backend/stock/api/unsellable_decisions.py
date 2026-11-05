"""Manager decisions consume non-sellable stock without a second available-stock loss."""
import uuid
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema, extend_schema_field
from stock.services.return_costs import current_return_cost, decision_cost, integral_unit_cost
from stock.api.return_cost_serializers import ReturnCostSerializer, cost_representation, ReceiptCostFields
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from common.api.fields import ExactIntegerField
from common.services.operations import json_value, original_receipt
from common.services.errors import OperationConflict
from common.models import Activity
from common.api.errors import OriginReferenceError
from common.services.origins import with_public_origin
from stock.models import NonSellableDecision, ReturnReceipt, StockMovement
from purchasing.models import Supplier
from sales.services.pricing import MAX_FCFA
from purchasing.api.purchases import EventSerializer
from stock.api.return_receipts import receipt_context
from stock.services.stock_alert_services import observe_stock
from purchasing.services.supplier_ledger import append_supplier_ledger
from stock.services.unsellable_balance import remaining_unsellable


class NonSellableQuantityConflict(APIException):
    status_code = 409
    default_code = "unsellable_quantity_changed"
    default_detail = _("La quantité non vendable restante a changé. Relire avant confirmation.")


class NonSellableDecisionInput(EventSerializer):
    kind = serializers.ChoiceField(choices=NonSellableDecision._meta.get_field("kind").choices)
    quantity_milli = ExactIntegerField(min_value=1, max_value=MAX_FCFA)
    supplier_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    is_sellable_confirmed = serializers.BooleanField(required=False, default=False)

    def validate(self, data):
        if data["kind"] == "restock" and not data["is_sellable_confirmed"]:
            raise serializers.ValidationError({"is_sellable_confirmed": _("Confirmer l'état vendable avant remise en stock.")})
        if data["kind"] != "restock" and data["is_sellable_confirmed"]:
            raise serializers.ValidationError({"is_sellable_confirmed": _("Confirmation vendable réservée à la remise en stock.")})
        if (data["kind"] == "supplier_return") != (data["supplier_id"] is not None):
            raise serializers.ValidationError({"supplier_id": _("Fournisseur requis uniquement pour un départ fournisseur.")})
        return data


class NonSellableDecisionSerializer(ReceiptCostFields, serializers.ModelSerializer):
    cost = serializers.SerializerMethodField()

    @extend_schema_field(ReturnCostSerializer(allow_null=True))
    def get_cost(self, obj):
        if 'creation_cost' in self.context:
            snapshot,total=self.context['creation_cost']
            return cost_representation(snapshot,total=total,override_total=True)
        snapshot, total = decision_cost(obj)
        return cost_representation(snapshot, total=total, override_total=True)
    receipt_id = serializers.UUIDField(read_only=True)
    operation_id = serializers.UUIDField(read_only=True)
    movement_id = serializers.UUIDField(read_only=True, allow_null=True)
    supplier_id = serializers.UUIDField(read_only=True, allow_null=True)
    actor_id = serializers.SerializerMethodField()
    reason = serializers.CharField(source="operation.reason", read_only=True)

    @extend_schema_field(serializers.UUIDField())
    def get_actor_id(self,obj):
        return str(self.context.get('origin_identity',{}).get('actor_id',getattr(obj,'public_actor_id',obj.operation.actor_id)))

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_unit_cost_micros(self,obj):
        projection=self.context.get('creation_projection')
        return projection['unit_cost_micros'] if projection is not None else super().get_unit_cost_micros(obj)

    def get_cost_is_provisional(self,obj) -> bool:
        projection=self.context.get('creation_projection')
        return projection['cost_is_provisional'] if projection is not None else super().get_cost_is_provisional(obj)
    def cost_receipt(self, obj):
        return obj.receipt

    class Meta:
        model = NonSellableDecision
        fields = ["id", "receipt_id", "operation_id", "actor_id", "reason", "kind", "quantity_milli", "after_quantity_milli",
            "unit_kind", "is_sellable_confirmed", "occurred_at", "created_at", "movement_id", "supplier_id",
            "unit_cost_micros", "cost_is_provisional", "cost"]
        read_only_fields = fields


def decision_context(kwargs, request):
    shop, sale, line = receipt_context(kwargs, request)
    receipt = get_object_or_404(ReturnReceipt, id=kwargs["receipt_id"], return_line=line, condition="unsellable")
    return shop, sale, line, receipt


@transaction.atomic
def decide_unsellable(kwargs, request, data):
    shop, sale, line, receipt = decision_context(kwargs, request)
    canonical = json_value({**data, "receipt_id": receipt.id, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "unsellable_decided", canonical)
    if original is not None:
        return original, False
    if NonSellableDecision.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    quantity = data["quantity_milli"]
    if line.unit_kind == "piece" and quantity % 1000:
        raise serializers.ValidationError({"quantity_milli": _("Une pièce doit avoir une quantité entière.")})
    before = remaining_unsellable(receipt)
    if quantity > before:
        raise NonSellableQuantityConflict()
    if data["occurred_at"] < receipt.occurred_at:
        raise serializers.ValidationError({"occurred_at": _("La décision ne peut pas précéder la réception physique.")})
    supplier = get_object_or_404(Supplier, shop=shop, id=data["supplier_id"]) if data["supplier_id"] else None
    return _persist_non_sellable_decision(shop=shop,line=line,receipt=receipt,actor=request.user,
        installation=request.device_session.installation,data=data,canonical=canonical,before=before,supplier=supplier)


def _persist_non_sellable_decision(*,shop,line,receipt,actor,installation,data,canonical,before,supplier,
        origin_capture=None,source_snapshot=None,recovery_review=None,cost_basis=None,cost_total=None):
    """Persist a checked disposition and its explicit source cost under the caller's transaction."""
    quantity=data['quantity_milli']
    if quantity>before:raise OperationConflict()
    operation = Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor,
        action="unsellable_decided", target_id=data["id"], reason=data["reason"], details={"request": canonical})
    context={}
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=operation,capture=origin_capture)
        context={'origin_identity':{'actor_id':origin_capture.origin_user_id},'creation_cost':(cost_basis,cost_total),
            'creation_projection':source_snapshot}
        operation.details['recovery']=json_value({'capture_id':origin_capture.id,'origin_user_id':origin_capture.origin_user_id,
            'origin_installation_id':origin_capture.origin_installation_id,'origin_recorded_at':origin_capture.origin_recorded_at,
            'origin_installation_verified':False,'source_snapshot':source_snapshot,'review':recovery_review})
    movement = None
    if data["kind"] == "restock":
        current_cost = current_return_cost(receipt) if origin_capture is None else cost_basis
        unit_cost = source_snapshot['unit_cost_micros'] if origin_capture is not None else receipt.unit_cost_micros if current_cost is None else integral_unit_cost({
            "unit_cost_micros_numerator": current_cost.unit_cost_micros_numerator,
            "unit_cost_micros_denominator": current_cost.unit_cost_micros_denominator})
        movement = StockMovement.objects.create(id=uuid.uuid5(data["id"], "unsellable-restock"), product_id=line.sale_line.product_id,
            actor=actor, operation=operation, kind="unsellable_restock", quantity_milli=quantity,
            unit_cost_micros=unit_cost, occurred_at=data["occurred_at"], reason=data["reason"])
    decision = NonSellableDecision.objects.create(id=data["id"], receipt=receipt, operation=operation,
        installation=installation, kind=data["kind"], quantity_milli=quantity,
        after_quantity_milli=before - quantity, unit_kind=line.unit_kind, is_sellable_confirmed=data["is_sellable_confirmed"],
        occurred_at=data["occurred_at"], supplier=supplier, movement=movement)
    if supplier is not None:
        append_supplier_ledger(shop, supplier, operation, decision.id, "unsellable_return", decision.occurred_at)
    if movement is not None:
        observe_stock([line.sale_line.product_id], operation)
    saved = json_value(NonSellableDecisionSerializer(decision,context=context).data)
    operation.details["response"] = saved
    operation.save(update_fields=["details", "updated_at"])
    return saved, True


class NonSellableDecisionViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = NonSellableDecision.objects.none()
    serializer_class = NonSellableDecisionSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        _, _, _, receipt = decision_context(self.kwargs, self.request)
        rows=NonSellableDecision.objects.filter(receipt=receipt).annotate(shop_id=F('receipt__return_line__sale_return__sale__shop_id'))
        return with_public_origin(rows,actor_path='operation__actor_id').select_related("operation", "receipt__cost_snapshot").order_by("occurred_at", "id")

    @extend_schema(responses={200:NonSellableDecisionSerializer(many=True),409:OriginReferenceError})
    @transaction.atomic
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(responses={200:NonSellableDecisionSerializer,409:OriginReferenceError})
    @transaction.atomic
    def retrieve(self,request,*args,**kwargs):return super().retrieve(request,*args,**kwargs)

    @extend_schema(request=NonSellableDecisionInput, responses={200: NonSellableDecisionSerializer, 201: NonSellableDecisionSerializer})
    def create(self, request, *args, **kwargs):
        decision_context(self.kwargs, request)
        serializer = NonSellableDecisionInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        saved, created = decide_unsellable(self.kwargs, request, serializer.validated_data)
        return Response(saved, status=201 if created else 200)
