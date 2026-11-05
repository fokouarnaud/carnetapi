"""Physical observations do not create money or overwrite an earlier observation."""
from django.db import transaction
from django.db.models import Case, CharField, F, When
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.response import Response

from identity.services.authentication import require_live_session
from common.api.fields import ExactIntegerField, StrictSerializer
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from common.services.errors import OperationConflict
from common.models import Activity
from cash.models import CashCount, CashMovement
from sales.models import SalePayment
from sales.services.pricing import MAX_FCFA


def expected_cash_at(shop_id, method, other_name, counted_at):
    rows = CashMovement.objects.filter(shop_id=shop_id, method=method, occurred_at__lte=counted_at)
    if method == "other":
        rows = rows.annotate(payment_name=Case(
            When(sale_payment__isnull=False, then=F("sale_payment__other_name")),
            When(customer_payment_part__isnull=False, then=F("customer_payment_part__other_name")),
            When(customer_refund__isnull=False, then=F("customer_refund__other_name")),
            When(supplier_payment_part__isnull=False, then=F("supplier_payment_part__other_name")),
            When(supplier_refund__isnull=False, then=F("supplier_refund__other_name")),
            When(sale_return_refund__isnull=False, then=F("sale_return_refund__other_name")),
            default=F("other_name"), output_field=CharField(),
        )).filter(payment_name=other_name)
    return sum(rows.values_list("amount_fcfa", flat=True).iterator(chunk_size=1000))


class CreateCashCountSerializer(StrictSerializer):
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    method = serializers.ChoiceField(choices=SalePayment.Method.choices)
    other_name = serializers.CharField(max_length=80, allow_blank=True, default="")
    physical_fcfa = ExactIntegerField(min_value=0, max_value=MAX_FCFA)
    counted_at = serializers.DateTimeField()
    reason = serializers.CharField(max_length=200, allow_blank=False)

    def validate(self, data):
        if (data["method"] == "other") != bool(data["other_name"]):
            raise serializers.ValidationError("Nom obligatoire uniquement pour le moyen autre.")
        return data

    def validate_counted_at(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("Comptage futur non autorisé.")
        return value


class CashCountSerializer(serializers.ModelSerializer):
    actor_id = serializers.UUIDField(read_only=True)
    installation_id = serializers.UUIDField(read_only=True)
    operation_id = serializers.UUIDField(read_only=True)
    recorded_at = serializers.DateTimeField(source="operation.created_at", read_only=True)
    expected_at_creation_fcfa = serializers.IntegerField(read_only=True)
    expected_fcfa = serializers.IntegerField(source="current_expected_fcfa", read_only=True)
    difference_fcfa = serializers.IntegerField(source="current_difference_fcfa", read_only=True)
    expected_revised = serializers.BooleanField(source="current_expected_revised", read_only=True)
    requires_regularization = serializers.BooleanField(source="current_requires_regularization", read_only=True)
    provisional = serializers.SerializerMethodField()

    def get_provisional(self, obj) -> bool:
        # A lease verification is not proof that every phone has uploaded its operations.
        return True

    def to_representation(self, instance):
        expected = expected_cash_at(instance.shop_id, instance.method, instance.other_name, instance.counted_at)
        instance.current_expected_fcfa = expected
        instance.current_difference_fcfa = instance.physical_fcfa - expected
        instance.current_expected_revised = expected != instance.expected_at_creation_fcfa
        instance.current_requires_regularization = instance.current_difference_fcfa != 0
        return super().to_representation(instance)

    class Meta:
        model = CashCount
        fields = ["id", "actor_id", "installation_id", "operation_id", "method", "other_name", "physical_fcfa",
                  "expected_at_creation_fcfa", "expected_fcfa", "difference_fcfa", "counted_at", "recorded_at", "reason",
                  "expected_revised", "requires_regularization", "provisional"]
        read_only_fields = fields


@transaction.atomic
def create_count(shop_id, actor, installation, session_id, data):
    require_live_session(actor, session_id, installation)
    shop = current_shop(shop_id, actor, installation)
    require_manager(shop, actor)
    canonical = json_value({**data, "installation_id": installation.id})
    original = original_receipt(data["operation_id"], shop, actor, "cash_count_created", canonical)
    if original is not None:
        return original, False
    if CashCount.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    expected = expected_cash_at(shop.id, data["method"], data["other_name"], data["counted_at"])
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor, action="cash_count_created",
                                    target_id=data["id"], reason=data["reason"], details={"request": canonical})
    fields = {key: value for key, value in data.items() if key != "operation_id"}
    count = CashCount.objects.create(shop=shop, actor=actor, installation=installation, operation=event,
                                     expected_at_creation_fcfa=expected, **fields)
    receipt = json_value(CashCountSerializer(count).data)
    event.details["response"] = receipt
    event.save(update_fields=["details", "updated_at"])
    return receipt, True


class CashCountViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = CashCount.objects.none()
    serializer_class = CashCountSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return CashCount.objects.none()
        require_live_session(self.request.user, self.request.device_session.id, self.request.device_session.installation)
        shop = current_shop(self.kwargs["shop_id"], self.request.user, self.request.device_session.installation)
        require_manager(shop, self.request.user)
        return CashCount.objects.filter(shop=shop).select_related("operation").order_by("counted_at", "id")

    @transaction.atomic
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @transaction.atomic
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(request=CreateCashCountSerializer, responses={200: CashCountSerializer, 201: CashCountSerializer})
    def create(self, request, *args, **kwargs):
        self.get_queryset()
        serializer = CreateCashCountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = create_count(self.kwargs["shop_id"], request.user, request.device_session.installation,
                                        request.device_session.id, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)
