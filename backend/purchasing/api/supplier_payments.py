"""Actual supplier payments and explicit noncash allocation of their advances."""
import uuid
from django.db import transaction
from purchasing.services.supplier_ledger import append_supplier_ledger
from django.db.models import Prefetch,F,Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema,extend_schema_field,extend_schema_view
from rest_framework import mixins, serializers, viewsets
from rest_framework.response import Response

from common.api.fields import ExactIntegerField, StrictSerializer
from common.services.operations import json_value, original_receipt
from common.services.errors import OperationConflict
from common.models import Activity
from common.services.origins import with_public_origin,public_identity,OriginReferenceConflict
from common.api.errors import OriginReferenceError
from cash.models import CashMovement
from purchasing.models import SupplierDebt, SupplierPayment, SupplierPaymentAllocation, SupplierPaymentPart
from sales.services.pricing import MAX_FCFA
from purchasing.api.purchases import EventSerializer, context
from sales.api.sale_serializers import DeclaredPaymentSerializer


class PaymentInput(EventSerializer):
    parts = DeclaredPaymentSerializer(many=True, allow_empty=False, max_length=50)

    def validate(self, data):
        if len({part["id"] for part in data["parts"]}) != len(data["parts"]):
            raise serializers.ValidationError({"parts": "UUID répété."})
        if sum(part["amount_fcfa"] for part in data["parts"]) > MAX_FCFA:
            raise serializers.ValidationError({"parts": "Total hors limites."})
        return data


class AllocationLineInput(StrictSerializer):
    debt_id = serializers.UUIDField()
    amount_fcfa = ExactIntegerField(min_value=1, max_value=MAX_FCFA)


class AllocationInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    allocations = AllocationLineInput(many=True, allow_empty=False, max_length=500)

    def validate_allocations(self, value):
        if len({row["debt_id"] for row in value}) != len(value):
            raise serializers.ValidationError("Dette répétée.")
        return value


class PaymentPartSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupplierPaymentPart
        fields = ["id", "method", "other_name", "external_reference", "amount_fcfa"]
        read_only_fields = fields


class AllocationSerializer(serializers.ModelSerializer):
    debt_id = serializers.UUIDField(read_only=True)
    operation_id = serializers.UUIDField(read_only=True)
    actor_id = serializers.SerializerMethodField()
    reason = serializers.CharField(source="operation.reason", read_only=True)
    recorded_at = serializers.DateTimeField(source="operation.created_at", read_only=True)

    @extend_schema_field(serializers.UUIDField())
    def get_actor_id(self,obj):
        return str(self.context.get('allocation_origin_identity',{}).get('actor_id',getattr(obj,'public_actor_id',obj.operation.actor_id)))

    class Meta:
        model = SupplierPaymentAllocation
        fields = ["id", "operation_id", "actor_id", "reason", "recorded_at", "debt_id", "amount_fcfa"]
        read_only_fields = fields


class PaymentSerializer(serializers.ModelSerializer):
    supplier_id = serializers.UUIDField(read_only=True)
    operation_id = serializers.UUIDField(read_only=True)
    installation_id = serializers.SerializerMethodField()
    parts = PaymentPartSerializer(many=True, read_only=True)
    allocations = AllocationSerializer(many=True, read_only=True)

    @extend_schema_field(serializers.UUIDField())
    def get_installation_id(self,obj):return public_identity(obj,self.context,'installation_id')

    class Meta:
        model = SupplierPayment
        fields = ["id", "supplier_id", "operation_id", "installation_id", "occurred_at", "created_at", "amount_fcfa",
                  "unallocated_fcfa", "parts", "allocations"]
        read_only_fields = fields


class AllocationReceiptSerializer(serializers.Serializer):
    operation_id = serializers.UUIDField()
    payment_id = serializers.UUIDField()
    allocated_fcfa = serializers.IntegerField()
    after_unallocated_fcfa = serializers.IntegerField()
    recorded_at = serializers.DateTimeField()
    allocations = AllocationSerializer(many=True)


@transaction.atomic
def create_payment(shop_id, supplier_id, request, data):
    shop, supplier = context(shop_id, supplier_id, request)
    canonical = json_value({**data, "supplier_id": supplier.id, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "supplier_payment_created", canonical)
    if original is not None:
        return original, False
    if (SupplierPayment.objects.filter(id=data["id"]).exists()
            or SupplierPaymentPart.objects.filter(id__in=[part["id"] for part in data["parts"]]).exists()
            or CashMovement.objects.filter(id__in=[uuid.uuid5(part['id'],'supplier-payment-cash') for part in data['parts']]).exists()):
        raise OperationConflict()
    return _persist_supplier_payment(shop=shop,supplier=supplier,actor=request.user,
        installation=request.device_session.installation,data=data,canonical=canonical)


def _persist_supplier_payment(*,shop,supplier,actor,installation,data,canonical,origin_capture=None,source_snapshot=None,recovery_review=None):
    """Keep actual money, advance, ledger and source in the caller's transaction."""
    amount = sum(part["amount_fcfa"] for part in data["parts"])
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor,
                                    action="supplier_payment_created", target_id=data["id"], reason=data["reason"], details={"request": canonical})
    receipt_context={}
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=event,capture=origin_capture)
        receipt_context['origin_identity']={'installation_id':origin_capture.origin_installation_id}
        event.details['recovery']=json_value({'capture_id':origin_capture.id,'origin_user_id':origin_capture.origin_user_id,
            'origin_installation_id':origin_capture.origin_installation_id,'origin_recorded_at':origin_capture.origin_recorded_at,
            'origin_installation_verified':False,'source_snapshot':source_snapshot,'review':recovery_review,
            'cash_identity_policy':'derived_from_payment_part'})
    payment = SupplierPayment.objects.create(id=data["id"], shop=shop, supplier=supplier, operation=event,
                                              installation=installation, occurred_at=data["occurred_at"],
                                              amount_fcfa=amount, unallocated_fcfa=amount)
    for position, part in enumerate(data["parts"]):
        source = SupplierPaymentPart.objects.create(payment=payment, position=position, **part)
        CashMovement.objects.create(id=uuid.uuid5(source.id,'supplier-payment-cash'),shop=shop,actor=actor,installation=installation,
                                    operation=event, supplier_payment_part=source, method=source.method,
                                    amount_fcfa=-source.amount_fcfa, occurred_at=data["occurred_at"], reason=data["reason"])
    receipt = json_value(PaymentSerializer(payment,context=receipt_context).data)
    event.details["response"] = receipt
    event.save(update_fields=["details", "updated_at"])
    append_supplier_ledger(shop, supplier, event, payment.id, "payment", payment.occurred_at,
                           advance_delta=payment.amount_fcfa)
    return receipt, True


@transaction.atomic
def allocate_payment(shop_id, supplier_id, payment_id, request, data):
    shop, supplier = context(shop_id, supplier_id, request)
    payment = get_object_or_404(SupplierPayment, shop=shop, supplier=supplier, operation__shop=shop,id=payment_id)
    canonical = json_value({**data, "payment_id": payment.id, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "supplier_payment_allocated", canonical)
    if original is not None:
        return original, False
    total = sum(row["amount_fcfa"] for row in data["allocations"])
    if total > payment.unallocated_fcfa:
        raise serializers.ValidationError({"allocations": "Affectation supérieure à l'avance disponible."})
    prepared = []
    for row in data["allocations"]:
        debts=SupplierDebt.objects.filter(shop=shop,supplier=supplier).filter(Q(document__isnull=True)|Q(document__shop=shop,document__supplier=supplier))
        debt=get_object_or_404(debts,id=row['debt_id'])
        if row["amount_fcfa"] > debt.remaining_fcfa:
            raise serializers.ValidationError({"allocations": "Affectation supérieure à la dette actuelle."})
        prepared.append((debt, row["amount_fcfa"]))
    return _persist_supplier_payment_allocation(shop=shop,supplier=supplier,payment=payment,actor=request.user,
        installation=request.device_session.installation,data=data,canonical=canonical,prepared=prepared)


def _persist_supplier_payment_allocation(*,shop,supplier,payment,actor,installation,data,canonical,prepared,
        source_row_ids=None,effective_at=None,origin_capture=None,source_snapshot=None,recovery_review=None):
    """Apply only selected exact debt amounts; the original money is never moved again."""
    total=sum(amount for _debt,amount in prepared)
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor, action="supplier_payment_allocated",
                                    target_id=payment.id, reason=data["reason"], details={"request": canonical})
    receipt_context={}
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=event,capture=origin_capture)
        receipt_context['allocation_origin_identity']={'actor_id':origin_capture.origin_user_id}
        event.details['recovery']=json_value({'capture_id':origin_capture.id,'origin_user_id':origin_capture.origin_user_id,
            'origin_installation_id':origin_capture.origin_installation_id,'origin_recorded_at':origin_capture.origin_recorded_at,
            'origin_installation_verified':False,'source_snapshot':source_snapshot,'review':recovery_review,
            'replay_before_unallocated_fcfa':payment.unallocated_fcfa,'replay_after_unallocated_fcfa':payment.unallocated_fcfa-total})
    allocations = []
    for position, (debt, amount) in enumerate(prepared):
        identity={'id':source_row_ids[position]} if source_row_ids is not None else {}
        allocations.append(SupplierPaymentAllocation.objects.create(payment=payment, debt=debt, operation=event,
                                                                      position=position, amount_fcfa=amount,**identity))
        debt.remaining_fcfa -= amount
        debt.save(update_fields=["remaining_fcfa", "updated_at"])
    payment.unallocated_fcfa -= total
    payment.save(update_fields=["unallocated_fcfa", "updated_at"])
    receipt = json_value(AllocationReceiptSerializer({"operation_id": event.id, "payment_id": payment.id,
                                                      "allocated_fcfa": total, "after_unallocated_fcfa": payment.unallocated_fcfa,
                                                      "recorded_at": event.created_at, "allocations": allocations},context=receipt_context).data)
    event.details["response"] = receipt
    event.save(update_fields=["details", "updated_at"])
    append_supplier_ledger(shop, supplier, event, event.id, "payment_allocation", effective_at or event.created_at,
                           debt_delta=-total, advance_delta=-total)
    return receipt, True


@extend_schema_view(list=extend_schema(responses={200:PaymentSerializer(many=True),409:OriginReferenceError}),
    retrieve=extend_schema(responses={200:PaymentSerializer,409:OriginReferenceError}))
class SupplierPaymentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = SupplierPayment.objects.none()
    serializer_class = PaymentSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        shop, supplier = context(self.kwargs["shop_id"], self.kwargs["supplier_id"], self.request)
        allocations=SupplierPaymentAllocation.objects.filter(payment__shop=shop,payment__supplier=supplier).annotate(shop_id=F('payment__shop_id'))
        if allocations.exclude(debt__shop=shop,debt__supplier=supplier).exists():raise OriginReferenceConflict()
        allocations=with_public_origin(allocations,actor_path='operation__actor_id',installation_path=None).select_related('operation')
        payments=with_public_origin(SupplierPayment.objects.filter(shop=shop,supplier=supplier),actor_path='operation__actor_id')
        return payments.prefetch_related(
            "parts", Prefetch("allocations", queryset=allocations),
        ).order_by("occurred_at", "id")

    @extend_schema(request=PaymentInput, responses={200: PaymentSerializer, 201: PaymentSerializer})
    def create(self, request, *args, **kwargs):
        context(self.kwargs["shop_id"], self.kwargs["supplier_id"], request)
        serializer = PaymentInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        result, created = create_payment(self.kwargs["shop_id"], self.kwargs["supplier_id"], request, serializer.validated_data)
        return Response(result, status=201 if created else 200)

    @extend_schema(request=AllocationInput, responses={200: AllocationReceiptSerializer, 201: AllocationReceiptSerializer})
    def allocate(self, request, *args, **kwargs):
        context(self.kwargs["shop_id"], self.kwargs["supplier_id"], request)
        serializer = AllocationInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        result, created = allocate_payment(self.kwargs["shop_id"], self.kwargs["supplier_id"], self.kwargs["pk"], request, serializer.validated_data)
        return Response(result, status=201 if created else 200)
