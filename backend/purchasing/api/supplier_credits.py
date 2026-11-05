"""Accepted credit, origin debt reduction, and actual cash refunds are distinct."""
from django.db import transaction
from purchasing.services.supplier_ledger import append_supplier_ledger
from django.db.models import Q,F
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema,extend_schema_field,extend_schema_view
from rest_framework import mixins, serializers, viewsets
from rest_framework.response import Response
from common.api.fields import ExactIntegerField
from common.services.operations import json_value, original_receipt
from common.services.errors import OperationConflict
from common.models import Activity
from common.services.origins import with_public_origin
from common.api.errors import OriginReferenceError
from cash.models import CashMovement
from purchasing.models import SupplierCredit, SupplierCreditAllocation, SupplierDebt, SupplierRefund, SupplierReturn
from sales.services.pricing import MAX_FCFA
from purchasing.api.purchases import EventSerializer, context
from sales.api.sale_serializers import DeclaredPaymentSerializer
from purchasing.api.supplier_payments import AllocationInput


def return_reference_fcfa(departure):
    """Round cumulative returned value half-up, then take the previous difference."""
    earlier = Q(supplier_return__created_at__lt=departure.created_at) | Q(
        supplier_return__created_at=departure.created_at, supplier_return_id__lt=departure.id,
    )
    reference = 0
    for part in departure.lines.select_related("line"):
        line = part.line
        previous = sum(line.return_lines.filter(earlier).values_list("quantity_milli", flat=True))
        def rounded(quantity):
            return (2 * line.total_fcfa * quantity + line.quantity_milli) // (2 * line.quantity_milli)
        reference += rounded(previous + part.quantity_milli) - rounded(previous)
    return reference


class CreditInput(EventSerializer):
    return_id = serializers.UUIDField()
    amount_fcfa = ExactIntegerField(min_value=1, max_value=MAX_FCFA)


class CreditSerializer(serializers.ModelSerializer):
    return_id = serializers.UUIDField(source="supplier_return_id", read_only=True, allow_null=True)
    actor_id = serializers.SerializerMethodField()
    reason = serializers.CharField(source="operation.reason", read_only=True)

    @extend_schema_field(serializers.UUIDField())
    def get_actor_id(self,obj):
        return str(self.context.get('origin_identity',{}).get('actor_id',getattr(obj,'public_actor_id',obj.operation.actor_id)))

    class Meta:
        model = SupplierCredit
        fields = ["id", "kind", "return_id", "occurred_at", "created_at", "actor_id", "reason", "reference_fcfa", "amount_fcfa",
                  "applied_to_origin_fcfa", "remaining_fcfa"]
        read_only_fields = fields


class OpeningBalanceInput(EventSerializer):
    balance_kind = serializers.ChoiceField(choices=["advance", "credit"])
    amount_fcfa = ExactIntegerField(min_value=1, max_value=MAX_FCFA)


def credits_for_supplier(shop, supplier):
    return SupplierCredit.objects.filter(
        Q(supplier_return__document__shop=shop, supplier_return__document__supplier=supplier)
        | Q(opening_shop=shop, opening_supplier=supplier))


@transaction.atomic
def open_supplier_balance(shop_id, supplier_id, request, data):
    shop, supplier = context(shop_id, supplier_id, request)
    canonical = json_value({**data, "supplier_id": supplier.id, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "supplier_balance_opened", canonical)
    if original is not None:
        return original, False
    if SupplierCredit.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    return _persist_supplier_opening_balance(shop=shop,supplier=supplier,actor=request.user,
        installation=request.device_session.installation,data=data,canonical=canonical)


def _persist_supplier_opening_balance(*,shop,supplier,actor,installation,data,canonical,
        origin_capture=None,source_snapshot=None,recovery_review=None):
    """Keep historical advance and credit separate from cash, debt and returns."""
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor, action="supplier_balance_opened",
                                    target_id=data["id"], reason=data["reason"], details={"request": canonical})
    context={}
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=event,capture=origin_capture)
        context['origin_identity']={'actor_id':origin_capture.origin_user_id}
        event.details['recovery']=json_value({'capture_id':origin_capture.id,'origin_user_id':origin_capture.origin_user_id,
            'origin_installation_id':origin_capture.origin_installation_id,'origin_recorded_at':origin_capture.origin_recorded_at,
            'origin_installation_verified':False,'source_snapshot':source_snapshot,'review':recovery_review})
    balance = SupplierCredit.objects.create(id=data["id"], kind="opening_" + data["balance_kind"],
        opening_shop=shop, opening_supplier=supplier, operation=event, installation=installation,
        occurred_at=data["occurred_at"], reference_fcfa=None, amount_fcfa=data["amount_fcfa"],
        applied_to_origin_fcfa=0, remaining_fcfa=data["amount_fcfa"])
    receipt = json_value(CreditSerializer(balance,context=context).data)
    event.details["response"] = receipt
    event.save(update_fields=["details", "updated_at"])
    append_supplier_ledger(shop, supplier, event, balance.id, balance.kind, balance.occurred_at,
        advance_delta=balance.amount_fcfa if balance.kind == "opening_advance" else 0,
        credit_delta=balance.amount_fcfa if balance.kind == "opening_credit" else 0)
    return receipt, True


class RefundInput(EventSerializer, DeclaredPaymentSerializer):
    pass


class RefundSerializer(serializers.ModelSerializer):
    credit_id = serializers.UUIDField(read_only=True)
    actor_id = serializers.UUIDField(source="operation.actor_id", read_only=True)
    reason = serializers.CharField(source="operation.reason", read_only=True)

    class Meta:
        model = SupplierRefund
        fields = ["id", "credit_id", "occurred_at", "created_at", "actor_id", "reason", "method", "other_name",
                  "external_reference", "amount_fcfa", "after_remaining_fcfa"]
        read_only_fields = fields


class CreditAllocationSerializer(serializers.ModelSerializer):
    debt_id = serializers.UUIDField(read_only=True)
    actor_id = serializers.UUIDField(source="operation.actor_id", read_only=True)
    reason = serializers.CharField(source="operation.reason", read_only=True)

    class Meta:
        model = SupplierCreditAllocation
        fields = ["id", "debt_id", "actor_id", "reason", "amount_fcfa"]
        read_only_fields = fields


class CreditAllocationReceipt(serializers.Serializer):
    operation_id = serializers.UUIDField()
    credit_id = serializers.UUIDField()
    after_remaining_fcfa = serializers.IntegerField()
    recorded_at = serializers.DateTimeField()
    allocations = CreditAllocationSerializer(many=True)


class CreditHistorySerializer(serializers.ModelSerializer):
    actor_id = serializers.UUIDField(source='public_actor_id',read_only=True)

    class Meta:
        model = Activity
        fields = ["id", "actor_id", "action", "reason", "details", "created_at"]
        read_only_fields = fields


@transaction.atomic
def accept_credit(shop_id, supplier_id, request, data):
    shop, supplier = context(shop_id, supplier_id, request)
    departure = get_object_or_404(SupplierReturn, id=data["return_id"], document__shop=shop, document__supplier=supplier)
    canonical = json_value({**data, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "supplier_credit_accepted", canonical)
    if original is not None:
        return original, False
    if SupplierCredit.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    debt = departure.document.debt
    applied = min(debt.remaining_fcfa, data["amount_fcfa"])
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=request.user, action="supplier_credit_accepted",
                                    target_id=data["id"], reason=data["reason"], details={"request": canonical, "origin_debt_id": str(debt.id),
                                                                                   "before_debt_fcfa": debt.remaining_fcfa})
    credit = SupplierCredit.objects.create(id=data["id"], supplier_return=departure, operation=event,
                                           installation=request.device_session.installation, occurred_at=data["occurred_at"],
                                           reference_fcfa=return_reference_fcfa(departure), amount_fcfa=data["amount_fcfa"],
                                           applied_to_origin_fcfa=applied, remaining_fcfa=data["amount_fcfa"] - applied)
    debt.remaining_fcfa -= applied
    debt.save(update_fields=["remaining_fcfa", "updated_at"])
    receipt = json_value(CreditSerializer(credit).data)
    event.details.update(response=receipt, after_debt_fcfa=debt.remaining_fcfa)
    event.save(update_fields=["details", "updated_at"])
    append_supplier_ledger(shop, supplier, event, credit.id, "return_credit", credit.occurred_at,
        debt_delta=-applied, credit_delta=credit.amount_fcfa - applied,
        external_reference=departure.document.external_reference)
    return receipt, True


@transaction.atomic
def receive_refund(shop_id, supplier_id, credit_id, request, data):
    shop, supplier = context(shop_id, supplier_id, request)
    credit = get_object_or_404(credits_for_supplier(shop, supplier), id=credit_id)
    canonical = json_value({**data, "credit_id": credit.id, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "supplier_refund_received", canonical)
    if original is not None:
        return original, False
    if SupplierRefund.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    if data["amount_fcfa"] > credit.remaining_fcfa:
        raise serializers.ValidationError({"amount_fcfa": "Remboursement supérieur au crédit disponible."})
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=request.user, action="supplier_refund_received",
                                    target_id=data["id"], reason=data["reason"], details={"request": canonical})
    credit.remaining_fcfa -= data["amount_fcfa"]
    credit.save(update_fields=["remaining_fcfa", "updated_at"])
    refund = SupplierRefund.objects.create(id=data["id"], credit=credit, operation=event,
                                           installation=request.device_session.installation, occurred_at=data["occurred_at"],
                                           method=data["method"], other_name=data["other_name"], external_reference=data["external_reference"],
                                           amount_fcfa=data["amount_fcfa"], after_remaining_fcfa=credit.remaining_fcfa)
    CashMovement.objects.create(shop=shop, actor=request.user, installation=request.device_session.installation,
                                operation=event, supplier_refund=refund, method=refund.method, amount_fcfa=refund.amount_fcfa,
                                occurred_at=refund.occurred_at, reason=data["reason"])
    receipt = json_value(RefundSerializer(refund).data)
    event.details["response"] = receipt
    event.save(update_fields=["details", "updated_at"])
    append_supplier_ledger(shop, supplier, event, refund.id, "supplier_refund", refund.occurred_at,
        advance_delta=-refund.amount_fcfa if credit.kind == "opening_advance" else 0,
        credit_delta=-refund.amount_fcfa if credit.kind != "opening_advance" else 0)
    return receipt, True


@transaction.atomic
def allocate_credit(shop_id, supplier_id, credit_id, request, data):
    shop, supplier = context(shop_id, supplier_id, request)
    credit = get_object_or_404(credits_for_supplier(shop, supplier), id=credit_id)
    canonical = json_value({**data, "credit_id": credit.id, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "supplier_credit_allocated", canonical)
    if original is not None:
        return original, False
    amount = sum(row["amount_fcfa"] for row in data["allocations"])
    if amount > credit.remaining_fcfa:
        raise serializers.ValidationError({"allocations": "Affectation supérieure au crédit disponible."})
    prepared = []
    for row in data["allocations"]:
        debt = get_object_or_404(SupplierDebt, id=row["debt_id"], shop=shop, supplier=supplier)
        if row["amount_fcfa"] > debt.remaining_fcfa:
            raise serializers.ValidationError({"allocations": "Affectation supérieure à la dette actuelle."})
        prepared.append((debt, row["amount_fcfa"]))
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=request.user, action="supplier_credit_allocated",
                                    target_id=credit.id, reason=data["reason"], details={"request": canonical})
    rows = []
    for position, (debt, applied) in enumerate(prepared):
        rows.append(SupplierCreditAllocation.objects.create(credit=credit, operation=event, debt=debt, position=position, amount_fcfa=applied))
        debt.remaining_fcfa -= applied
        debt.save(update_fields=["remaining_fcfa", "updated_at"])
    credit.remaining_fcfa -= amount
    credit.save(update_fields=["remaining_fcfa", "updated_at"])
    receipt = json_value(CreditAllocationReceipt({"operation_id": event.id, "credit_id": credit.id,
                                                 "after_remaining_fcfa": credit.remaining_fcfa, "recorded_at": event.created_at,
                                                 "allocations": rows}).data)
    event.details["response"] = receipt
    event.save(update_fields=["details", "updated_at"])
    is_advance = credit.kind == "opening_advance"
    append_supplier_ledger(shop, supplier, event, event.id, "advance_allocation" if is_advance else "credit_allocation",
        event.created_at, debt_delta=-amount, advance_delta=-amount if is_advance else 0,
        credit_delta=-amount if not is_advance else 0)
    return receipt, True


@extend_schema_view(list=extend_schema(responses={200:CreditSerializer(many=True),409:OriginReferenceError}),
    retrieve=extend_schema(responses={200:CreditSerializer,409:OriginReferenceError}))
class SupplierCreditViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = SupplierCredit.objects.none()
    serializer_class = CreditSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        shop, supplier = context(self.kwargs["shop_id"], self.kwargs["supplier_id"], self.request)
        rows=credits_for_supplier(shop,supplier).annotate(shop_id=Coalesce(F('opening_shop_id'),F('supplier_return__document__shop_id')))
        return with_public_origin(rows,actor_path='operation__actor_id').select_related('operation').order_by('created_at','id')

    @extend_schema(request=CreditInput, responses={200: CreditSerializer, 201: CreditSerializer})
    def create(self, request, *args, **kwargs):
        context(self.kwargs["shop_id"], self.kwargs["supplier_id"], request)
        serializer = CreditInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = accept_credit(self.kwargs["shop_id"], self.kwargs["supplier_id"], request, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)

    @extend_schema(request=RefundInput, responses={200: RefundSerializer, 201: RefundSerializer})
    def refund(self, request, *args, **kwargs):
        context(self.kwargs["shop_id"], self.kwargs["supplier_id"], request)
        serializer = RefundInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = receive_refund(self.kwargs["shop_id"], self.kwargs["supplier_id"], self.kwargs["pk"], request, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)

    @extend_schema(request=AllocationInput, responses={200: CreditAllocationReceipt, 201: CreditAllocationReceipt})
    def allocate(self, request, *args, **kwargs):
        context(self.kwargs["shop_id"], self.kwargs["supplier_id"], request)
        serializer = AllocationInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = allocate_credit(self.kwargs["shop_id"], self.kwargs["supplier_id"], self.kwargs["pk"], request, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)

    @extend_schema(responses={200:CreditHistorySerializer(many=True),409:OriginReferenceError})
    @transaction.atomic
    def history(self, request, *args, **kwargs):
        credit = self.get_object()
        events = Activity.objects.filter(
            Q(supplier_credit=credit) | Q(supplier_refund__credit=credit) | Q(supplier_credit_allocations__credit=credit),
            shop_id=credit.shop_id,
        ).distinct().order_by("created_at", "id")
        events=with_public_origin(events,operation_path='',installation_path=None)
        page = self.paginate_queryset(events)
        return self.get_paginated_response(CreditHistorySerializer(page, many=True).data)


    @extend_schema(request=OpeningBalanceInput, responses={200: CreditSerializer, 201: CreditSerializer})
    def create_opening(self, request, *args, **kwargs):
        context(self.kwargs["shop_id"], self.kwargs["supplier_id"], request)
        serializer = OpeningBalanceInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = open_supplier_balance(self.kwargs["shop_id"], self.kwargs["supplier_id"], request, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)

    @extend_schema(responses={200:CreditSerializer(many=True),409:OriginReferenceError})
    @transaction.atomic
    def list_openings(self, request, *args, **kwargs):
        balances = self.get_queryset().filter(kind__in=["opening_advance", "opening_credit"])
        page = self.paginate_queryset(balances)
        return self.get_paginated_response(CreditSerializer(page, many=True).data)
