"""Manager-only previews and append-only purchase cost allocation decisions."""
from django.db import transaction
from django.db.models import OuterRef, Prefetch, Subquery
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.response import Response

from common.api.fields import ExactIntegerField, StrictSerializer
from purchasing.services.charge_allocation import allocate_charge, ChargeWeight, ChargeAllocationFailure
from common.services.operations import json_value, original_receipt
from common.services.errors import OperationConflict
from common.models import Activity
from purchasing.models import PurchaseDocument, PurchaseLine, PurchaseCharge, PurchaseChargeRevision, PurchaseChargeAllocation
from sales.services.pricing import MAX_FCFA
from purchasing.api.purchases import context, EventSerializer
from purchasing.services.purchase_costs import historical_costs_need_review, current_charge_cost_review_pending


class ChargeAllocationInput(StrictSerializer):
    line_id = serializers.UUIDField()
    amount_fcfa = ExactIntegerField(min_value=0, max_value=MAX_FCFA)


class AllocationPlanInput(StrictSerializer):
    allocations = ChargeAllocationInput(many=True, allow_empty=False, max_length=500)

    def validate_allocations(self, value):
        if len({row["line_id"] for row in value}) != len(value):
            raise ValidationError({"code": "duplicate_charge_line"})
        return value


class ChargeInput(EventSerializer, AllocationPlanInput):
    label = serializers.CharField(max_length=150, allow_blank=False)
    amount_fcfa = ExactIntegerField(min_value=0, max_value=MAX_FCFA)


class ChargeRevisionInput(AllocationPlanInput):
    operation_id = serializers.UUIDField()
    base_version = ExactIntegerField(min_value=1, max_value=MAX_FCFA - 1)
    reason = serializers.CharField(max_length=2000, allow_blank=False)


class ChargePreviewInput(StrictSerializer):
    amount_fcfa = ExactIntegerField(min_value=0, max_value=MAX_FCFA)
    line_ids = serializers.ListField(child=serializers.UUIDField(), allow_empty=False, max_length=500, required=False)
    allocations = ChargeAllocationInput(many=True, allow_empty=False, max_length=500, required=False)


class ChargePreviewSerializer(serializers.Serializer):
    amount_fcfa = serializers.IntegerField()
    allocations = ChargeAllocationInput(many=True)


class ChargeRevisionSerializer(serializers.ModelSerializer):
    historical_costs_pending_review = serializers.SerializerMethodField()

    def get_historical_costs_pending_review(self, obj) -> bool:
        return current_charge_cost_review_pending(obj)
    operation_id = serializers.UUIDField(read_only=True)
    actor_id = serializers.UUIDField(source="operation.actor_id", read_only=True)
    reason = serializers.CharField(source="operation.reason", read_only=True)
    allocations = ChargeAllocationInput(many=True, read_only=True)

    class Meta:
        model = PurchaseChargeRevision
        fields = ["id", "operation_id", "actor_id", "reason", "version", "supersedes_id", "created_at", "allocations", "historical_costs_pending_review"]
        read_only_fields = fields


class ChargeSerializer(serializers.ModelSerializer):
    document_id = serializers.UUIDField(read_only=True)
    operation_id = serializers.UUIDField(read_only=True)
    current_revision = serializers.SerializerMethodField()

    @extend_schema_field(ChargeRevisionSerializer(allow_null=True))
    def get_current_revision(self, obj):
        if hasattr(obj, "latest_revisions"):
            revision = obj.latest_revisions[0] if obj.latest_revisions else None
        else:
            revision = obj.revisions.select_related("operation").prefetch_related("allocations").order_by("-version").first()
        return ChargeRevisionSerializer(revision).data if revision is not None else None

    class Meta:
        model = PurchaseCharge
        fields = ["id", "document_id", "operation_id", "label", "amount_fcfa", "occurred_at", "created_at", "current_revision"]
        read_only_fields = fields


def validated_plan(document, amount, rows):
    ids = [row["line_id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValidationError({"code": "duplicate_charge_line"})
    lines = {line.id: line for line in PurchaseLine.objects.filter(document=document, id__in=ids)}
    if len(lines) != len(ids):
        raise NotFound()
    if sum(row["amount_fcfa"] for row in rows) != amount:
        raise ValidationError({"code": "charge_allocation_total_mismatch", "expected_fcfa": amount})
    return [(lines[row["line_id"]], row["amount_fcfa"]) for row in rows]


class ChargeContext:
    def document(self):
        shop, supplier = context(self.kwargs["shop_id"], self.kwargs["supplier_id"], self.request)
        return get_object_or_404(PurchaseDocument, shop=shop, supplier=supplier, kind="invoice", id=self.kwargs["document_id"])


class PurchaseChargeViewSet(ChargeContext, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = PurchaseCharge.objects.none()
    serializer_class = ChargeSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        document = self.document()
        latest = PurchaseChargeRevision.objects.filter(charge_id=OuterRef("charge_id")).order_by("-version").values("version")[:1]
        revisions = PurchaseChargeRevision.objects.filter(version=Subquery(latest)).select_related("operation").prefetch_related("allocations")
        return PurchaseCharge.objects.filter(document=document).prefetch_related(Prefetch("revisions", queryset=revisions,
            to_attr="latest_revisions")).order_by("occurred_at", "id")

    @transaction.atomic
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @transaction.atomic
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(request=ChargePreviewInput, responses=ChargePreviewSerializer)
    @transaction.atomic
    def preview(self, request, *args, **kwargs):
        document = self.document()
        serializer = ChargePreviewInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if "allocations" in data:
            rows = data["allocations"]
            validated_plan(document, data["amount_fcfa"], rows)
            if "line_ids" in data and set(data["line_ids"]) != {row["line_id"] for row in rows}:
                raise ValidationError({"code": "charge_selection_mismatch"})
        else:
            lines = document.lines.all()
            if "line_ids" in data:
                ids = data["line_ids"]
                if len(set(ids)) != len(ids):
                    raise ValidationError({"code": "duplicate_charge_line"})
                lines = list(lines.filter(id__in=ids))
                if len(lines) != len(ids):
                    raise NotFound()
            result = allocate_charge(data["amount_fcfa"], [ChargeWeight(line.id, line.total_fcfa) for line in lines])
            if isinstance(result, ChargeAllocationFailure):
                raise ValidationError({"code": result.code, "line_ids": [str(value) for value in result.line_ids]})
            rows = [{"line_id": row.line_id, "amount_fcfa": row.amount_fcfa} for row in result.allocations]
        return Response(ChargePreviewSerializer({"amount_fcfa": data["amount_fcfa"], "allocations": rows}).data)

    @extend_schema(request=ChargeInput, responses={200: ChargeSerializer, 201: ChargeSerializer})
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        document = self.document()
        serializer = ChargeInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        canonical = json_value({**data, "document_id": document.id, "installation_id": request.device_session.installation.id})
        original = original_receipt(data["operation_id"], document.shop, request.user, "purchase_charge_recorded", canonical)
        if original is not None:
            return Response(original, status=200)
        if PurchaseCharge.objects.filter(id=data["id"]).exists():
            raise OperationConflict()
        plan = validated_plan(document, data["amount_fcfa"], data["allocations"])
        event = Activity.objects.create(id=data["operation_id"], shop=document.shop, actor=request.user,
            action="purchase_charge_recorded", target_id=data["id"], reason=data["reason"], details={"request": canonical,
                "historical_costs_pending_review": historical_costs_need_review([line.id for line, _amount in plan])})
        charge = PurchaseCharge.objects.create(id=data["id"], document=document, operation=event,
            installation=request.device_session.installation, occurred_at=data["occurred_at"], label=data["label"], amount_fcfa=data["amount_fcfa"])
        revision = PurchaseChargeRevision.objects.create(charge=charge, operation=event, version=1)
        PurchaseChargeAllocation.objects.bulk_create([PurchaseChargeAllocation(revision=revision, line=line, amount_fcfa=amount) for line, amount in plan])
        result = json_value(ChargeSerializer(charge).data)
        event.details["response"] = result
        event.save(update_fields=["details", "updated_at"])
        return Response(result, status=201)


class PurchaseChargeRevisionViewSet(ChargeContext, mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = PurchaseChargeRevision.objects.none()
    serializer_class = ChargeRevisionSerializer

    def charge(self):
        return get_object_or_404(PurchaseCharge, document=self.document(), id=self.kwargs["charge_id"])

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        return PurchaseChargeRevision.objects.filter(charge=self.charge()).select_related("operation").prefetch_related("allocations").order_by("version", "id")

    @transaction.atomic
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(request=ChargeRevisionInput, responses={200: ChargeRevisionSerializer, 201: ChargeRevisionSerializer})
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        charge = self.charge()
        serializer = ChargeRevisionInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        canonical = json_value({**data, "charge_id": charge.id, "installation_id": request.device_session.installation.id})
        original = original_receipt(data["operation_id"], charge.document.shop, request.user, "purchase_charge_reallocated", canonical)
        if original is not None:
            return Response(original, status=200)
        previous = charge.revisions.order_by("-version").first()
        if previous.version != data["base_version"]:
            error = APIException({"code": "purchase_charge_version_conflict"})
            error.status_code = 409
            raise error
        plan = validated_plan(charge.document, charge.amount_fcfa, data["allocations"])
        event = Activity.objects.create(id=data["operation_id"], shop=charge.document.shop, actor=request.user,
            action="purchase_charge_reallocated", target_id=charge.id, reason=data["reason"], details={"request": canonical,
                "historical_costs_pending_review": historical_costs_need_review(
                    {line.id for line, _amount in plan} | set(previous.allocations.values_list("line_id", flat=True)))})
        revision = PurchaseChargeRevision.objects.create(charge=charge, operation=event, version=previous.version + 1, supersedes=previous)
        PurchaseChargeAllocation.objects.bulk_create([PurchaseChargeAllocation(revision=revision, line=line, amount_fcfa=amount) for line, amount in plan])
        result = json_value(ChargeRevisionSerializer(revision).data)
        event.details["response"] = result
        event.save(update_fields=["details", "updated_at"])
        return Response(result, status=201)
