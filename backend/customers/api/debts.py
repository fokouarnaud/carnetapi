"""Receivable reads use current permissions, not a sale's historic permissions."""
from zoneinfo import ZoneInfo

from django.shortcuts import get_object_or_404
from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from identity.services.access import effective_permissions
from common.services.operations import current_shop
from common.services.origins import with_public_origin
from common.api.errors import OriginReferenceError
from common.api.fields import ExactIntegerField, StrictSerializer
from customers.services.debt_services import manager_debt_context, open_debt, update_debt_due_date
from common.models import Activity
from customers.models import Customer, CustomerDebt


class OpenDebtSerializer(StrictSerializer):
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    amount_fcfa = ExactIntegerField(min_value=1, max_value=9223372036854775807)
    occurred_at = serializers.DateTimeField()
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    due_date = serializers.DateField(required=False, allow_null=True, default=None)

    def validate_occurred_at(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("La dette d'ouverture ne peut pas être datée dans le futur.")
        return value


class UpdateDebtDueSerializer(StrictSerializer):
    operation_id = serializers.UUIDField()
    base_version = ExactIntegerField(min_value=1, max_value=9223372036854775806)
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    due_date = serializers.DateField(allow_null=True)


class DebtHistorySerializer(serializers.ModelSerializer):
    actor_id = serializers.UUIDField(source="public_actor_id",read_only=True)

    class Meta:
        model = Activity
        fields = ["id", "actor_id", "action", "reason", "created_at", "details"]


class CustomerDebtSerializer(serializers.ModelSerializer):
    shop_id = serializers.UUIDField(read_only=True)
    customer_id = serializers.UUIDField(read_only=True)
    sale_id = serializers.UUIDField(read_only=True, allow_null=True)
    is_overdue = serializers.SerializerMethodField()

    class Meta:
        model = CustomerDebt
        fields = ["id", "shop_id", "customer_id", "sale_id", "amount_fcfa", "remaining_fcfa", "occurred_at", "due_at", "due_version", "is_overdue"]
        read_only_fields = fields

    def get_is_overdue(self, obj) -> bool:
        if obj.due_at is None or obj.remaining_fcfa == 0:
            return False
        local_zone = ZoneInfo(obj.shop.timezone)
        return timezone.now().astimezone(local_zone).date() > obj.due_at.astimezone(local_zone).date()


class CustomerDebtViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = CustomerDebt.objects.none()
    serializer_class = CustomerDebtSerializer

    def manager_context(self):
        return manager_debt_context(self.kwargs["shop_id"], self.kwargs["client_id"], self.request.user,
                                    self.request.device_session.installation)

    @extend_schema(request=OpenDebtSerializer, responses={200: CustomerDebtSerializer, 201: CustomerDebtSerializer})
    def create(self, request, *args, **kwargs):
        self.manager_context()
        serializer = OpenDebtSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = open_debt(self.kwargs["shop_id"], self.kwargs["client_id"], request.user,
                                     request.device_session.installation, request.device_session.id, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)

    @extend_schema(request=UpdateDebtDueSerializer, responses=CustomerDebtSerializer)
    def due_date(self, request, *args, **kwargs):
        self.manager_context()
        self.get_object()
        serializer = UpdateDebtDueSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(update_debt_due_date(
            self.kwargs["shop_id"], self.kwargs["client_id"], self.kwargs["pk"], request.user,
            request.device_session.installation, request.device_session.id, serializer.validated_data,
        ))

    @extend_schema(responses={200:DebtHistorySerializer(many=True),409:OriginReferenceError})
    def history(self, request, *args, **kwargs):
        shop, _ = self.manager_context()
        debt = self.get_object()
        events = Activity.objects.filter(shop=shop).filter(
            Q(target_id=debt.id, action__in=["customer_debt_opened", "customer_debt_due_changed"])
            | Q(id=debt.operation_id, action="sale_created"),
        ).order_by("created_at", "id")
        page = self.paginate_queryset(with_public_origin(events,operation_path="",installation_path=None))
        return self.get_paginated_response(DebtHistorySerializer(page, many=True).data)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return CustomerDebt.objects.none()
        shop = current_shop(self.kwargs["shop_id"], self.request.user, self.request.device_session.installation)
        customer = get_object_or_404(Customer, shop=shop, id=self.kwargs["client_id"])
        _, permissions = effective_permissions(shop, self.request.user)
        if not permissions["can_read_debt"]:
            raise PermissionDenied("Consultation des dettes non autorisée.")
        return CustomerDebt.objects.filter(shop=shop, customer=customer).select_related("shop").order_by("occurred_at", "id")
