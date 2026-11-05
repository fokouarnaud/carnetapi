"""Explicit refunds and allocations consume a return claim once, under a write lock."""
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema,extend_schema_field
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from identity.services.authentication import require_live_session
from common.services.operations import json_value, original_receipt
from common.services.errors import OperationConflict
from common.api.errors import OriginReferenceError
from common.services.origins import with_public_origin
from customers.services.ledger import append_customer_ledger
from common.models import Activity
from sales.models import SaleReturn,SalePayment
from customers.models import ReturnCreditSettlement, ReturnCreditAllocation, CustomerDebt
from cash.models import CashMovement
from customers.api.payment_serializers import CreateAllocationSerializer
from customers.services.payment_services import payment_context
from sales.services.pricing import MAX_FCFA
from purchasing.api.purchases import EventSerializer
from customers.services.return_credit_balance import remaining_return_credit, with_return_credit
from sales.api.sale_returns import return_context
from sales.api.sale_serializers import DeclaredPaymentSerializer


class ReturnCreditConflict(APIException):
    status_code = 409
    default_code = "return_credit_changed"
    default_detail = _("Le crédit du retour ou la dette a changé. Relire avant confirmation.")


class ReturnCreditRefundInput(EventSerializer, DeclaredPaymentSerializer):
    pass


class ReturnCreditAllocationInput(EventSerializer):
    allocations = CreateAllocationSerializer(many=True, allow_empty=False, max_length=500)

    def validate_allocations(self, value):
        if (len({row["id"] for row in value}) != len(value)
                or len({row["debt_id"] for row in value}) != len(value)
                or sum(row["amount_fcfa"] for row in value) > MAX_FCFA):
            raise serializers.ValidationError(_("Affectations répétées ou montant hors capacité."))
        return value


class ReturnCreditAllocationSerializer(serializers.ModelSerializer):
    debt_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = ReturnCreditAllocation
        fields = ["id", "debt_id", "amount_fcfa"]
        read_only_fields = fields


class ReturnCreditSettlementSerializer(serializers.ModelSerializer):
    sale_return_id = serializers.UUIDField(read_only=True)
    operation_id = serializers.UUIDField(read_only=True)
    actor_id = serializers.SerializerMethodField()
    reason = serializers.CharField(source="operation.reason", read_only=True)
    kind = serializers.CharField(read_only=True)
    allocations = ReturnCreditAllocationSerializer(many=True, read_only=True)
    method = serializers.ChoiceField(choices=SalePayment.Method.choices,allow_blank=True,read_only=True)

    @extend_schema_field(serializers.UUIDField())
    def get_actor_id(self,obj):
        return str(self.context.get("origin_identity",{}).get("actor_id",getattr(obj,"public_actor_id",obj.operation.actor_id)))

    class Meta:
        model = ReturnCreditSettlement
        fields = ["id", "sale_return_id", "operation_id", "actor_id", "reason", "kind", "amount_fcfa",
            "after_credit_fcfa", "occurred_at", "created_at", "method", "other_name", "external_reference", "allocations"]
        read_only_fields = fields


def settlement_context(kwargs, request):
    shop, sale = return_context(kwargs["shop_id"], kwargs["sale_id"], request)
    returned = get_object_or_404(SaleReturn, sale=sale, id=kwargs["return_id"])
    return shop, sale, returned


@transaction.atomic
def settle_return_credit(kwargs, request, data, kind):
    shop, sale, returned = settlement_context(kwargs, request)
    canonical = json_value({**data, "sale_return_id": returned.id, "kind": kind,
        "installation_id": request.device_session.installation.id})
    action = "return_credit_" + kind
    original = original_receipt(data["operation_id"], shop, request.user, action, canonical)
    if original is not None:
        return original, False
    if (ReturnCreditSettlement.objects.filter(id=data["id"]).exists()
            or (kind == "allocation" and ReturnCreditAllocation.objects.filter(id__in=[row["id"] for row in data["allocations"]]).exists())):
        raise OperationConflict()
    return _persist_return_credit_settlement(shop=shop,sale=sale,returned=returned,actor=request.user,
        installation=request.device_session.installation,data=data,kind=kind,canonical=canonical)


def _persist_return_credit_settlement(*,shop,sale,returned,actor,installation,data,kind,canonical,
        origin_capture=None,source_snapshot=None,recovery_review=None):
    """Persist checked facts under the caller's current authority and transaction."""
    action="return_credit_"+kind
    if data["occurred_at"] < returned.occurred_at:
        raise serializers.ValidationError({"occurred_at": _("Le règlement ne peut pas précéder le retour.")})
    before = remaining_return_credit(returned)
    amount = data["amount_fcfa"] if kind == "refund" else sum(row["amount_fcfa"] for row in data["allocations"])
    if amount > before:
        raise ReturnCreditConflict()
    debts = []
    if kind == "allocation":
        if sale.customer_id is None:
            raise serializers.ValidationError(_("Client d'origine requis pour affecter ce crédit à une dette."))
        for row in data["allocations"]:
            debt = get_object_or_404(CustomerDebt, shop=shop, customer_id=sale.customer_id, id=row["debt_id"])
            if data["occurred_at"] < debt.occurred_at:
                raise serializers.ValidationError({"occurred_at": _("L'affectation ne peut pas précéder la dette.")})
            if row["amount_fcfa"] > debt.remaining_fcfa:
                raise ReturnCreditConflict()
            debts.append((row, debt))
    operation = Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor, action=action,
        target_id=data["id"], reason=data["reason"], details={"request": canonical, "before_credit_fcfa": before,
        "after_credit_fcfa": before - amount})
    context={}
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=operation,capture=origin_capture)
        context["origin_identity"]={"actor_id":origin_capture.origin_user_id}
        operation.details["recovery"]=json_value({"capture_id":origin_capture.id,"origin_user_id":origin_capture.origin_user_id,
            "origin_installation_id":origin_capture.origin_installation_id,"origin_recorded_at":origin_capture.origin_recorded_at,
            "origin_installation_verified":False,"source_snapshot":source_snapshot,"review":recovery_review})
    decision = ReturnCreditSettlement.objects.create(id=data["id"], sale_return=returned, operation=operation,
        installation=installation, kind=kind, amount_fcfa=amount, after_credit_fcfa=before - amount,
        occurred_at=data["occurred_at"], method=data.get("method", ""), other_name=data.get("other_name", ""),
        external_reference=data.get("external_reference", ""))
    for row, debt in debts:
        ReturnCreditAllocation.objects.create(id=row["id"], settlement=decision, debt=debt, amount_fcfa=row["amount_fcfa"])
        debt.remaining_fcfa -= row["amount_fcfa"]
        debt.save(update_fields=["remaining_fcfa", "updated_at"])
    if kind == "refund":
        CashMovement.objects.create(shop=shop, actor=actor, installation=installation,
            operation=operation, sale_return_refund=decision, method=decision.method, amount_fcfa=-amount,
            other_name=decision.other_name, external_reference=decision.external_reference,
            occurred_at=decision.occurred_at, reason=data["reason"])
    if sale.customer_id is not None:
        append_customer_ledger(shop, sale.customer, operation, decision.id, action, decision.occurred_at,
            amount if kind == "refund" else 0, debt_delta_fcfa=-amount if kind == "allocation" else 0, credit_delta_fcfa=-amount)
    receipt = json_value(ReturnCreditSettlementSerializer(decision,context=context).data)
    operation.details["response"] = receipt
    operation.save(update_fields=["details", "updated_at"])
    return receipt, True


class ReturnCreditSettlementViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = ReturnCreditSettlement.objects.none()
    serializer_class = ReturnCreditSettlementSerializer
    kind = "refund"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        _, _, returned = settlement_context(self.kwargs, self.request)
        rows=ReturnCreditSettlement.objects.filter(sale_return=returned,kind=self.kind).annotate(shop_id=F("sale_return__sale__shop_id"))
        return with_public_origin(rows,actor_path="operation__actor_id").select_related("operation").prefetch_related("allocations").order_by("occurred_at","id")

    @extend_schema(responses={200:ReturnCreditSettlementSerializer(many=True),409:OriginReferenceError})
    @transaction.atomic
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(responses={200:ReturnCreditSettlementSerializer,409:OriginReferenceError})
    @transaction.atomic
    def retrieve(self,request,*args,**kwargs):
        return super().retrieve(request,*args,**kwargs)

    def write(self, request, serializer_class):
        settlement_context(self.kwargs, request)
        serializer = serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = settle_return_credit(self.kwargs, request, serializer.validated_data, self.kind)
        return Response(receipt, status=201 if created else 200)

    @extend_schema(request=ReturnCreditRefundInput, responses={200: ReturnCreditSettlementSerializer, 201: ReturnCreditSettlementSerializer})
    def refund(self, request, *args, **kwargs):
        return self.write(request, ReturnCreditRefundInput)

    @extend_schema(request=ReturnCreditAllocationInput, responses={200: ReturnCreditSettlementSerializer, 201: ReturnCreditSettlementSerializer})
    def allocate(self, request, *args, **kwargs):
        return self.write(request, ReturnCreditAllocationInput)


class ReturnCreditSummarySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    sale_id = serializers.UUIDField()
    amount_fcfa = serializers.IntegerField(source="credit_fcfa")
    remaining_fcfa = serializers.SerializerMethodField()
    occurred_at = serializers.DateTimeField()

    def get_remaining_fcfa(self, obj) -> int:
        return remaining_return_credit(obj)


class ClientReturnCreditViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = SaleReturn.objects.none()
    serializer_class = ReturnCreditSummarySerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        request = self.request
        require_live_session(request.user, request.device_session.id, request.device_session.installation)
        shop, customer = payment_context(self.kwargs["shop_id"], self.kwargs["client_id"], request.user, request.device_session.installation)
        return with_return_credit(SaleReturn.objects.filter(sale__shop=shop, sale__customer=customer, credit_fcfa__gt=0)).order_by("occurred_at", "id")

    @transaction.atomic
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)
