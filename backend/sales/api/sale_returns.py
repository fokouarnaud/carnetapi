"""Manager returns preserve original invoices, cash and distinct physical receipts."""
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema,extend_schema_field
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from identity.services.authentication import require_live_session
from common.api.fields import ExactIntegerField, StrictSerializer
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from common.services.errors import OperationConflict
from common.api.errors import OriginReferenceError
from common.services.origins import with_public_origin
from customers.services.ledger import append_customer_ledger
from common.models import Activity
from sales.models import Sale, SaleLine, SaleReturn, SaleReturnLine, SaleCancellation
from customers.models import CustomerDebt
from sales.services.pricing import MAX_FCFA
from purchasing.api.purchases import EventSerializer
from sales.services.return_pricing import return_amount_fcfa
from customers.services.return_credit_balance import remaining_return_credit, with_return_credit
from sales.services.sale_state import SaleCancelledConflict


class ReturnQuantityConflict(APIException):
    status_code = 409
    default_code = "return_quantity_exceeded"
    default_detail = _("La quantité cumulée retournée dépasse la vente d'origine.")


class ReturnLineInput(StrictSerializer):
    id = serializers.UUIDField()
    sale_line_id = serializers.UUIDField()
    quantity_milli = ExactIntegerField(min_value=1, max_value=MAX_FCFA)


class ReturnInput(EventSerializer):
    lines = ReturnLineInput(many=True, allow_empty=False, max_length=500)

    def validate_lines(self, value):
        if (len({row["id"] for row in value}) != len(value)
                or len({row["sale_line_id"] for row in value}) != len(value)):
            raise serializers.ValidationError(_("Ligne de retour répétée."))
        return value


class SaleReturnLineSerializer(serializers.ModelSerializer):
    sale_line_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = SaleReturnLine
        fields = ["id", "sale_line_id", "quantity_milli", "amount_fcfa", "original_quantity_milli", "original_amount_fcfa", "unit_kind"]
        read_only_fields = fields


class SaleReturnSerializer(serializers.ModelSerializer):
    remaining_credit_fcfa = serializers.SerializerMethodField()

    def get_remaining_credit_fcfa(self, obj) -> int:
        return remaining_return_credit(obj)

    sale_id = serializers.UUIDField(read_only=True)
    operation_id = serializers.UUIDField(read_only=True)
    actor_id = serializers.SerializerMethodField()
    reason = serializers.CharField(source="operation.reason", read_only=True)
    lines = SaleReturnLineSerializer(many=True, read_only=True)

    @extend_schema_field(serializers.UUIDField())
    def get_actor_id(self,obj):
        return str(self.context.get("origin_identity",{}).get("actor_id",getattr(obj,"public_actor_id",obj.operation.actor_id)))

    class Meta:
        model = SaleReturn
        fields = ["id", "sale_id", "operation_id", "actor_id", "reason", "occurred_at", "created_at",
                  "amount_fcfa", "debt_reduced_fcfa", "credit_fcfa", "remaining_credit_fcfa", "lines"]
        read_only_fields = fields


def return_context(shop_id, sale_id, request):
    require_live_session(request.user, request.device_session.id, request.device_session.installation)
    shop = current_shop(shop_id, request.user, request.device_session.installation)
    require_manager(shop, request.user)
    return shop, get_object_or_404(Sale, shop=shop, id=sale_id)


@transaction.atomic
def record_return(shop_id, sale_id, request, data):
    shop, sale = return_context(shop_id, sale_id, request)
    canonical = json_value({**data, "sale_id": sale.id, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "sale_return_created", canonical)
    if original is not None:
        return original, False
    if SaleCancellation.objects.filter(sale=sale).exists():
        raise SaleCancelledConflict()
    if (SaleReturn.objects.filter(id=data["id"]).exists()
            or SaleReturnLine.objects.filter(id__in=[row["id"] for row in data["lines"]]).exists()):
        raise OperationConflict()
    if data["occurred_at"] < sale.occurred_at:
        raise serializers.ValidationError({"occurred_at": _("Le retour ne peut pas précéder la vente.")})
    snapshots = []
    for row in data["lines"]:
        origin = get_object_or_404(SaleLine, sale=sale, id=row["sale_line_id"])
        quantity = row["quantity_milli"]
        if origin.unit_kind == "piece" and quantity % 1000:
            raise serializers.ValidationError({"quantity_milli": _("Une pièce doit avoir une quantité entière.")})
        before = sum(origin.return_lines.values_list("quantity_milli", flat=True).iterator(chunk_size=1000))
        if before + quantity > origin.base_quantity_milli:
            raise ReturnQuantityConflict()
        amount = return_amount_fcfa(origin.net_fcfa, origin.base_quantity_milli, before, quantity)
        snapshots.append({"id": row["id"], "sale_line": origin, "quantity_milli": quantity, "amount_fcfa": amount,
                          "original_quantity_milli": origin.base_quantity_milli, "original_amount_fcfa": origin.net_fcfa,
                          "unit_kind": origin.unit_kind})
    debt = CustomerDebt.objects.filter(sale=sale).first()
    return _persist_sale_return(shop=shop,sale=sale,actor=request.user,installation=request.device_session.installation,
        data=data,canonical=canonical,snapshots=snapshots,debt=debt)


def _persist_sale_return(*,shop,sale,actor,installation,data,canonical,snapshots,debt,
        origin_capture=None,source_snapshot=None,recovery_review=None):
    """Persist checked original-price returns under the caller's transaction."""
    total = sum(row["amount_fcfa"] for row in snapshots)
    reduced = min(total, debt.remaining_fcfa) if debt else 0
    if source_snapshot is not None and (total!=source_snapshot["amount_fcfa"] or reduced!=source_snapshot["debt_reduced_fcfa"]
            or total-reduced!=source_snapshot["credit_fcfa"]):raise OperationConflict()
    operation = Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor,
        action="sale_return_created", target_id=data["id"], reason=data["reason"], details={"request": canonical})
    context={}
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=operation,capture=origin_capture)
        context["origin_identity"]={"actor_id":origin_capture.origin_user_id}
        operation.details["recovery"]=json_value({"capture_id":origin_capture.id,"origin_user_id":origin_capture.origin_user_id,
            "origin_installation_id":origin_capture.origin_installation_id,"origin_recorded_at":origin_capture.origin_recorded_at,
            "origin_installation_verified":False,"source_snapshot":source_snapshot,"review":recovery_review})
    returned = SaleReturn.objects.create(id=data["id"], sale=sale, operation=operation,
        installation=installation, occurred_at=data["occurred_at"],
        amount_fcfa=total, debt_reduced_fcfa=reduced, credit_fcfa=total - reduced)
    for row in snapshots:
        SaleReturnLine.objects.create(sale_return=returned, **row)
    if reduced:
        debt.remaining_fcfa -= reduced
        debt.save(update_fields=["remaining_fcfa", "updated_at"])
    if sale.customer_id is not None:
        append_customer_ledger(shop, sale.customer, operation, returned.id, "sale_return", returned.occurred_at,
            -total, debt_delta_fcfa=-reduced, credit_delta_fcfa=total - reduced)
    receipt = json_value(SaleReturnSerializer(returned,context=context).data)
    operation.details["response"] = receipt
    operation.save(update_fields=["details", "updated_at"])
    return receipt, True


class SaleReturnViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = SaleReturn.objects.none()
    serializer_class = SaleReturnSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        _, sale = return_context(self.kwargs["shop_id"], self.kwargs["sale_id"], self.request)
        rows=with_return_credit(SaleReturn.objects.filter(sale=sale)).annotate(shop_id=F("sale__shop_id"))
        return with_public_origin(rows,actor_path="operation__actor_id").select_related("operation").prefetch_related("lines").order_by("occurred_at","id")

    @extend_schema(responses={200:SaleReturnSerializer(many=True),409:OriginReferenceError})
    @transaction.atomic
    def list(self,request,*args,**kwargs):return super().list(request,*args,**kwargs)

    @extend_schema(responses={200:SaleReturnSerializer,409:OriginReferenceError})
    @transaction.atomic
    def retrieve(self,request,*args,**kwargs):return super().retrieve(request,*args,**kwargs)

    @extend_schema(request=ReturnInput, responses={200: SaleReturnSerializer, 201: SaleReturnSerializer})
    def create(self, request, *args, **kwargs):
        return_context(self.kwargs["shop_id"], self.kwargs["sale_id"], request)
        serializer = ReturnInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = record_return(self.kwargs["shop_id"], self.kwargs["sale_id"], request, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)
