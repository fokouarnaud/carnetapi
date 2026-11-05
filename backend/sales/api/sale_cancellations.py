"""Cancel a sale through an explicit state event and a return of its remaining lines."""
import uuid
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema,extend_schema_field
from rest_framework import serializers, viewsets
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from common.services.operations import json_value, original_receipt
from common.services.errors import OperationConflict
from common.models import Activity
from common.api.errors import OriginReferenceError
from common.services.origins import with_public_origin
from sales.models import SaleCancellation, SaleReturn, SaleReturnLine
from purchasing.api.purchases import EventSerializer
from sales.api.sale_returns import record_return, return_context
from sales.services.sale_state import SaleCancelledConflict
from stock.services.stock_queries import StockTotal


class CancellationTimelineConflict(APIException):
    status_code = 409
    default_code = "cancellation_timeline_conflict"
    default_detail = _("L'annulation précède un retour déjà enregistré. Vérifier la chronologie.")


class SaleCancellationSerializer(serializers.ModelSerializer):
    sale_id = serializers.UUIDField(read_only=True)
    operation_id = serializers.UUIDField(read_only=True)
    sale_return_id = serializers.UUIDField(read_only=True, allow_null=True)
    actor_id = serializers.SerializerMethodField()
    reason = serializers.CharField(source="operation.reason", read_only=True)

    @extend_schema_field(serializers.UUIDField())
    def get_actor_id(self,obj):
        return str(self.context.get('origin_identity',{}).get('actor_id',getattr(obj,'public_actor_id',obj.operation.actor_id)))

    class Meta:
        model = SaleCancellation
        fields = ["id", "sale_id", "operation_id", "actor_id", "reason", "occurred_at", "created_at", "sale_return_id"]
        read_only_fields = fields


@transaction.atomic
def cancel_sale(shop_id, sale_id, request, data):
    shop, sale = return_context(shop_id, sale_id, request)
    canonical = json_value({**data, "sale_id": sale.id, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "sale_cancelled", canonical)
    if original is not None:
        return original, False
    if SaleCancellation.objects.filter(sale=sale).exists():
        raise SaleCancelledConflict()
    if SaleCancellation.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    if data["occurred_at"] < sale.occurred_at:
        raise serializers.ValidationError({"occurred_at": _("L'annulation ne peut pas précéder la vente.")})
    if sale.returns.filter(occurred_at__gt=data["occurred_at"]).exists():
        raise CancellationTimelineConflict()
    child_operation_id = uuid.uuid5(data["id"], "cancellation-return-operation")
    if child_operation_id == data["operation_id"]:
        raise OperationConflict()
    before = {row["sale_line_id"]: int(row["total"]) for row in SaleReturnLine.objects.filter(sale_line__sale=sale)
        .order_by().values("sale_line_id").annotate(total=StockTotal("quantity_milli"))}
    lines = []
    for line in sale.lines.all():
        remaining = line.base_quantity_milli - before.get(line.id, 0)
        if remaining < 0:
            raise OperationConflict()
        if remaining:
            lines.append({"id": uuid.uuid5(data["id"], "cancellation-line:" + str(line.id)),
                          "sale_line_id": line.id, "quantity_milli": remaining})
    def append_return():
        return_id = uuid.uuid5(data["id"], "cancellation-return")
        record_return(shop_id, sale.id, request, {"id": return_id, "operation_id": child_operation_id,
            "occurred_at": data["occurred_at"], "reason": data["reason"], "lines": lines})
        return SaleReturn.objects.get(id=return_id, sale=sale),[]
    return _persist_sale_cancellation(shop=shop,sale=sale,actor=request.user,installation=request.device_session.installation,
        data=data,canonical=canonical,return_writer=append_return if lines else None)


def _persist_sale_cancellation(*,shop,sale,actor,installation,data,canonical,return_writer=None,
        origin_capture=None,source_snapshot=None,recovery_review=None):
    """Keep parent and compensating writes under the caller's atomic boundary."""
    operation = Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor,
        action="sale_cancelled", target_id=sale.id, reason=data["reason"], details={"request": canonical})
    context={}
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=operation,capture=origin_capture)
        context['origin_identity']={'actor_id':origin_capture.origin_user_id}
        operation.details['recovery']=json_value({'capture_id':origin_capture.id,'origin_user_id':origin_capture.origin_user_id,
            'origin_installation_id':origin_capture.origin_installation_id,'origin_recorded_at':origin_capture.origin_recorded_at,
            'origin_installation_verified':False,'source_snapshot':source_snapshot,'review':recovery_review})
    compensating,related=return_writer() if return_writer is not None else (None,[])
    cancellation = SaleCancellation.objects.create(id=data["id"], sale=sale, operation=operation,
        installation=installation, sale_return=compensating, occurred_at=data["occurred_at"])
    receipt = json_value(SaleCancellationSerializer(cancellation,context=context).data)
    operation.details["response"] = receipt
    if origin_capture is not None:operation.details['recovery']['related_integration_decision_ids']=related
    operation.save(update_fields=["details", "updated_at"])
    return receipt, True


class SaleCancellationViewSet(viewsets.GenericViewSet):
    queryset = SaleCancellation.objects.none()
    serializer_class = SaleCancellationSerializer

    @extend_schema(responses={200:SaleCancellationSerializer,409:OriginReferenceError})
    @transaction.atomic
    def retrieve(self, request, *args, **kwargs):
        _, sale = return_context(self.kwargs["shop_id"], self.kwargs["sale_id"], request)
        rows=SaleCancellation.objects.filter(sale=sale).annotate(shop_id=F('sale__shop_id'))
        cancellation = get_object_or_404(with_public_origin(rows,actor_path='operation__actor_id').select_related('operation'))
        return Response(SaleCancellationSerializer(cancellation).data)

    @extend_schema(request=EventSerializer, responses={200: SaleCancellationSerializer, 201: SaleCancellationSerializer})
    def create(self, request, *args, **kwargs):
        return_context(self.kwargs["shop_id"], self.kwargs["sale_id"], request)
        serializer = EventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = cancel_sale(self.kwargs["shop_id"], self.kwargs["sale_id"], request, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)
