"""Real cash events, separated from revenue and restricted before aggregation."""
from django.db import transaction
from django.db.models import Case, CharField, F, When
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.utils.urls import replace_query_param

from identity.services.access import effective_permissions
from identity.services.authentication import require_live_session
from common.api.fields import StrictSerializer
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from common.services.errors import OperationConflict
from common.models import Activity
from cash.models import CashMovement
from sales.api.sale_serializers import DeclaredPaymentSerializer
from backups.api.recovery_metadata import RecoveryReportSerializer
from common.services.origins import public_identity,with_public_origin
from common.api.errors import OriginReferenceError
from drf_spectacular.utils import extend_schema_field


class CreateCashSerializer(DeclaredPaymentSerializer):
    operation_id = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=CashMovement._meta.get_field("manual_kind").choices)
    occurred_at = serializers.DateTimeField()
    reason = serializers.CharField(max_length=200, allow_blank=False)

    def validate_occurred_at(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("Date future non autorisée.")
        return value


class CashSerializer(serializers.ModelSerializer):
    actor_id = serializers.SerializerMethodField()
    installation_id = serializers.SerializerMethodField()
    operation_id = serializers.UUIDField(read_only=True)
    recorded_at = serializers.DateTimeField(source="operation.created_at", read_only=True)
    kind = serializers.SerializerMethodField()
    other_name = serializers.SerializerMethodField()
    external_reference = serializers.SerializerMethodField()

    @extend_schema_field(serializers.UUIDField())
    def get_actor_id(self,obj):return public_identity(obj,self.context,"actor_id")

    @extend_schema_field(serializers.UUIDField())
    def get_installation_id(self,obj):return public_identity(obj,self.context,"installation_id")

    def get_kind(self, obj) -> str:
        if obj.manual_kind:
            return obj.manual_kind
        if obj.sale_payment_id:
            return "sale_payment"
        if obj.customer_payment_part_id:
            return "customer_collection"
        if obj.customer_refund_id:
            return "customer_refund"
        if obj.supplier_payment_part_id:
            return "supplier_payment"
        if obj.supplier_refund_id:
            return "supplier_refund"
        if obj.sale_return_refund_id:
            return "sale_return_refund"
        return "unknown"

    def payment_source(self, obj):
        return obj.sale_payment or obj.customer_payment_part or obj.customer_refund or obj.supplier_payment_part or obj.supplier_refund or obj.sale_return_refund or obj

    def get_other_name(self, obj) -> str:
        return self.payment_source(obj).other_name

    def get_external_reference(self, obj) -> str:
        return self.payment_source(obj).external_reference

    class Meta:
        model = CashMovement
        fields = ["id", "actor_id", "installation_id", "operation_id", "kind", "method", "other_name",
                  "external_reference", "amount_fcfa", "occurred_at", "recorded_at", "reason"]
        read_only_fields = fields


class CashQuerySerializer(StrictSerializer):
    scope = serializers.ChoiceField(choices=["global", "personal"], required=False)
    as_of = serializers.DateTimeField(required=False)
    page = serializers.IntegerField(min_value=1, required=False)
    page_size = serializers.IntegerField(min_value=1, required=False)

    def validate_as_of(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("Instantané futur non autorisé.")
        return value


class CashLedgerSerializer(RecoveryReportSerializer):
    count = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    as_of = serializers.DateTimeField()
    scope = serializers.CharField()
    totals_by_method_fcfa = serializers.DictField(child=serializers.IntegerField())
    provisional = serializers.BooleanField()
    results = CashSerializer(many=True)


@transaction.atomic
def create_cash(shop_id, actor, installation, session_id, data):
    require_live_session(actor, session_id, installation)
    shop = current_shop(shop_id, actor, installation)
    require_manager(shop, actor)
    canonical = json_value({**data, "installation_id": installation.id})
    original = original_receipt(data["operation_id"], shop, actor, "cash_manual_created", canonical)
    if original is not None:
        return original, False
    if CashMovement.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor,
                                    action="cash_manual_created", target_id=data["id"], reason=data["reason"],
                                    details={"request": canonical})
    signed_amount = -data["amount_fcfa"] if data["kind"] in ("withdrawal", "expense") else data["amount_fcfa"]
    movement = CashMovement.objects.create(id=data["id"], shop=shop, actor=actor, installation=installation, operation=event,
                                           manual_kind=data["kind"], method=data["method"], other_name=data["other_name"],
                                           external_reference=data["external_reference"], amount_fcfa=signed_amount,
                                           occurred_at=data["occurred_at"], reason=data["reason"])
    receipt = json_value(CashSerializer(movement).data)
    event.details["response"] = receipt
    event.save(update_fields=["details", "updated_at"])
    return receipt, True


class CashViewSet(viewsets.GenericViewSet):
    queryset = CashMovement.objects.none()
    serializer_class = CashSerializer

    @extend_schema(parameters=[CashQuerySerializer], responses={200:CashLedgerSerializer,409:OriginReferenceError},description=(
        "Read the current authorized shop's cash ledger; global scope requires current can_read_cash_balance "
        "permission, otherwise the default personal scope contains only this user's movements and totals. "
        "SQL pages and exact totals share the same scope and as_of cutoff, retained by page links. "
        "provisional stays true while device completeness is unproven. restoration describes live recovery "
        "for this shop, independently of as_of, without device identities or added cash permissions. "
        "Unknown inventory is null; zero observations never proves complete recovery."))
    @transaction.atomic
    def list(self, request, *args, **kwargs):
        require_live_session(request.user, request.device_session.id, request.device_session.installation)
        shop = current_shop(self.kwargs["shop_id"], request.user, request.device_session.installation)
        _, permissions = effective_permissions(shop, request.user)
        query = CashQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        scope = query.validated_data.get("scope", "global" if permissions["can_read_cash_balance"] else "personal")
        if scope == "global" and not permissions["can_read_cash_balance"]:
            raise PermissionDenied("Consultation du solde global non autorisée.")
        cutoff = query.validated_data.get("as_of", timezone.now())
        rows = with_public_origin(CashMovement.objects.filter(shop=shop, operation__created_at__lte=cutoff))
        if scope == "personal":
            rows = rows.filter(public_actor_id=request.user.id)
        totals = {}
        named = rows.annotate(payment_name=Case(
            When(sale_payment__isnull=False, then=F("sale_payment__other_name")),
            When(customer_payment_part__isnull=False, then=F("customer_payment_part__other_name")),
            When(customer_refund__isnull=False, then=F("customer_refund__other_name")),
            When(supplier_payment_part__isnull=False, then=F("supplier_payment_part__other_name")),
            When(supplier_refund__isnull=False, then=F("supplier_refund__other_name")),
            When(sale_return_refund__isnull=False, then=F("sale_return_refund__other_name")),
            default=F("other_name"), output_field=CharField(),
        ))
        for method, name, amount in named.values_list("method", "payment_name", "amount_fcfa").iterator(chunk_size=1000):
            key = "other:" + name if method == "other" else method
            totals[key] = totals.get(key, 0) + amount
        page = self.paginate_queryset(rows.select_related("operation", "sale_payment", "customer_payment_part", "customer_refund", "supplier_payment_part", "supplier_refund", "sale_return_refund")
                                      .order_by("occurred_at", "operation__created_at", "id"))
        stamp = serializers.DateTimeField().to_representation(cutoff)
        next_link, previous_link = self.paginator.get_next_link(), self.paginator.get_previous_link()
        def snapshot_link(link):
            return replace_query_param(replace_query_param(link, "as_of", stamp), "scope", scope) if link else None
        return Response(CashLedgerSerializer({
            "count": self.paginator.page.paginator.count, "next": snapshot_link(next_link), "previous": snapshot_link(previous_link),
            "as_of": cutoff, "scope": scope, "totals_by_method_fcfa": totals, "provisional": True, "results": page,
        },context={"shop":shop}).data)

    @extend_schema(request=CreateCashSerializer, responses={200: CashSerializer, 201: CashSerializer})
    def create(self, request, *args, **kwargs):
        shop = current_shop(self.kwargs["shop_id"], request.user, request.device_session.installation)
        require_manager(shop, request.user)
        serializer = CreateCashSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = create_cash(self.kwargs["shop_id"], request.user, request.device_session.installation,
                                       request.device_session.id, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)
