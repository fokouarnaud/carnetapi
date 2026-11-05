"""Motivated manager decisions on existing money, with immutable audit receipts."""
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema,extend_schema_field
from rest_framework import serializers, viewsets
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from identity.services.authentication import require_live_session
from common.api.fields import StrictSerializer
from common.api.errors import OriginReferenceError
from common.services.origins import with_public_origin
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from common.services.errors import OperationConflict
from common.models import Activity
from cash.models import CashMovement
from customers.models import Customer, CustomerCredit, CustomerDebt, CustomerPayment, PaymentRegularization, RegularizationAllocation
from customers.api.payment_serializers import CreateAllocationSerializer
from sales.services.pricing import MAX_FCFA
from sales.api.sale_serializers import DeclaredPaymentSerializer
from sales.models import SalePayment
from customers.services.ledger import append_customer_ledger


class AllocateUnallocatedSerializer(StrictSerializer):
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    allocations = CreateAllocationSerializer(many=True, allow_empty=False, max_length=500)

    def validate(self, data):
        ids = [row["id"] for row in data["allocations"]]
        if len(ids) != len(set(ids)) or sum(row["amount_fcfa"] for row in data["allocations"]) > MAX_FCFA:
            raise serializers.ValidationError("UUID répété ou montant hors capacité.")
        return data


class RefundUnallocatedSerializer(DeclaredPaymentSerializer):
    operation_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000, allow_blank=False)


class RegularizationAllocationSerializer(serializers.ModelSerializer):
    debt_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = RegularizationAllocation
        fields = ["id", "debt_id", "amount_fcfa"]
        read_only_fields = fields


class PaymentRegularizationSerializer(serializers.ModelSerializer):
    payment_id = serializers.UUIDField(read_only=True, allow_null=True)
    credit_id = serializers.UUIDField(read_only=True, allow_null=True)
    operation_id = serializers.UUIDField(read_only=True)
    actor_id = serializers.SerializerMethodField()
    reason = serializers.CharField(source="operation.reason", read_only=True)
    allocations = RegularizationAllocationSerializer(many=True, read_only=True)
    method = serializers.ChoiceField(choices=SalePayment.Method.choices,allow_blank=True,read_only=True)

    class Meta:
        model = PaymentRegularization
        fields = ["id", "payment_id", "credit_id", "operation_id", "actor_id", "kind", "reason", "amount_fcfa",
                  "after_unallocated_fcfa", "occurred_at", "method", "other_name", "external_reference", "allocations"]
        read_only_fields = fields

    @extend_schema_field(serializers.UUIDField())
    def get_actor_id(self,obj):
        return str(self.context.get("origin_identity",{}).get("actor_id",getattr(obj,"public_actor_id",obj.operation.actor_id)))


class RegularizationConflict(APIException):
    status_code = 409
    default_code = "regularization_conflict"
    default_detail = "Montant disponible ou dette modifié. Relire avant de confirmer la décision."


def regularization_context(shop_id, customer_id, payment_id, actor, installation, source_kind="payment"):
    shop = current_shop(shop_id, actor, installation)
    require_manager(shop, actor)
    customer = get_object_or_404(Customer, shop=shop, id=customer_id)
    model = CustomerCredit if source_kind == "credit" else CustomerPayment
    payment = get_object_or_404(model, shop=shop, customer=customer, id=payment_id)
    return shop, customer, payment


@transaction.atomic
def regularize_payment(shop_id, customer_id, payment_id, actor, installation, session_id, kind, data, source_kind="payment"):
    require_live_session(actor, session_id, installation)
    shop, customer, payment = regularization_context(shop_id, customer_id, payment_id, actor, installation, source_kind)
    canonical = json_value({**data, source_kind + "_id": payment.id, "kind": kind, "installation_id": installation.id})
    action = "customer_" + source_kind + "_" + kind
    original = original_receipt(data["operation_id"], shop, actor, action, canonical)
    if original is not None:
        return original, False
    if (PaymentRegularization.objects.filter(id=data["id"]).exists()
            or (kind == "allocation" and RegularizationAllocation.objects.filter(id__in=[row["id"] for row in data["allocations"]]).exists())):
        raise OperationConflict()
    return _persist_regularization(shop=shop,customer=customer,payment=payment,actor=actor,installation=installation,
        kind=kind,data=data,source_kind=source_kind,canonical=canonical,effective_at=timezone.now())


def _persist_regularization(*,shop,customer,payment,actor,installation,kind,data,source_kind,canonical,effective_at,
        origin_capture=None,recovery_review=None,source_snapshot=None):
    """Persist checked decisions under the caller's current rights and transaction."""
    action="customer_"+source_kind+"_"+kind
    amount = data["amount_fcfa"] if kind == "refund" else sum(row["amount_fcfa"] for row in data["allocations"])
    balance_field = "remaining_fcfa" if source_kind == "credit" else "unallocated_fcfa"
    before = getattr(payment, balance_field)
    if amount > before:
        raise RegularizationConflict()
    now = effective_at
    audit = Activity.objects.create(
        id=data["operation_id"], shop=shop, actor=actor, action=action, target_id=payment.id, reason=data["reason"],
        details={"request": canonical, "before_unallocated_fcfa": before, "after_unallocated_fcfa": before - amount},
    )
    context={}
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=audit,capture=origin_capture)
        context["origin_identity"]={"actor_id":origin_capture.origin_user_id}
        audit.details["recovery"]=json_value({"capture_id":origin_capture.id,"origin_user_id":origin_capture.origin_user_id,
            "origin_installation_id":origin_capture.origin_installation_id,"origin_recorded_at":origin_capture.origin_recorded_at,
            "origin_installation_verified":False,"source_snapshot":source_snapshot,"review":recovery_review})
    regularization = PaymentRegularization.objects.create(
        id=data["id"], **{source_kind: payment}, operation=audit, installation=installation, kind=kind,
        amount_fcfa=amount, after_unallocated_fcfa=before - amount, occurred_at=now,
        method=data.get("method", ""), other_name=data.get("other_name", ""), external_reference=data.get("external_reference", ""),
    )
    if kind == "allocation":
        for position, row in enumerate(data["allocations"]):
            debt = get_object_or_404(CustomerDebt, shop=shop, customer=customer, id=row["debt_id"])
            if row["amount_fcfa"] > debt.remaining_fcfa:
                raise RegularizationConflict()
            RegularizationAllocation.objects.create(id=row["id"], regularization=regularization, debt=debt,
                                                    position=position, amount_fcfa=row["amount_fcfa"])
            debt.remaining_fcfa -= row["amount_fcfa"]
            debt.save(update_fields=["remaining_fcfa", "updated_at"])
    else:
        CashMovement.objects.create(
            shop=shop, actor=actor, installation=installation, operation=audit, customer_refund=regularization,
            method=data["method"], amount_fcfa=-amount, occurred_at=now, reason="Remboursement client réel",
        )
    setattr(payment, balance_field, before - amount)
    fields = [balance_field, "updated_at"]
    if source_kind == "payment":
        payment.requires_regularization = payment.unallocated_fcfa > 0
        fields.append("requires_regularization")
    payment.save(update_fields=fields)
    receipt = json_value(PaymentRegularizationSerializer(regularization,context=context).data)
    audit.details["response"] = receipt
    audit.save(update_fields=["details", "updated_at"])
    append_customer_ledger(shop, customer, audit, regularization.id, kind, now,
                           amount if kind == "refund" else 0,
                           debt_delta_fcfa=-amount if kind == "allocation" else 0, credit_delta_fcfa=-amount)
    return receipt, True


class PaymentRegularizationViewSet(viewsets.GenericViewSet):
    queryset = PaymentRegularization.objects.none()
    serializer_class = PaymentRegularizationSerializer
    source_kind = "payment"

    def context(self):
        return regularization_context(self.kwargs["shop_id"], self.kwargs["client_id"], self.kwargs["payment_id"],
                                      self.request.user, self.request.device_session.installation, self.source_kind)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return PaymentRegularization.objects.none()
        _, _, payment = self.context()
        rows=PaymentRegularization.objects.filter(**{self.source_kind: payment}).annotate(shop_id=F(self.source_kind+"__shop_id"))
        return with_public_origin(rows,actor_path="operation__actor_id").select_related("operation").prefetch_related("allocations").order_by("created_at", "id")

    @extend_schema(responses={200:PaymentRegularizationSerializer(many=True),409:OriginReferenceError})
    @transaction.atomic
    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(PaymentRegularizationSerializer(page, many=True).data)

    def write(self, request, kind, serializer_class):
        self.context()
        serializer = serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = regularize_payment(
            self.kwargs["shop_id"], self.kwargs["client_id"], self.kwargs["payment_id"], request.user,
            request.device_session.installation, request.device_session.id, kind, serializer.validated_data, self.source_kind,
        )
        return Response(receipt, status=201 if created else 200)

    @extend_schema(request=AllocateUnallocatedSerializer, responses={200: PaymentRegularizationSerializer, 201: PaymentRegularizationSerializer})
    def allocate(self, request, *args, **kwargs):
        return self.write(request, "allocation", AllocateUnallocatedSerializer)

    @extend_schema(request=RefundUnallocatedSerializer, responses={200: PaymentRegularizationSerializer, 201: PaymentRegularizationSerializer})
    def refund(self, request, *args, **kwargs):
        return self.write(request, "refund", RefundUnallocatedSerializer)


class CreditRegularizationViewSet(PaymentRegularizationViewSet):
    source_kind = "credit"
