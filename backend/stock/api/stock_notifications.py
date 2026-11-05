"""Authorized notification candidates; delivery and system permission belong to Android."""
from django.db import transaction
from django.db.models import F
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.response import Response
from identity.services.authentication import require_live_session
from common.api.fields import StrictBoolean, StrictSerializer
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from common.models import Activity
from stock.models import StockAlertEvent, StockNotificationPreference


def notification_context(request, shop_id):
    session = request.device_session
    require_live_session(request.user, session.id, session.installation)
    shop = current_shop(shop_id, request.user, session.installation)
    require_manager(shop, request.user)
    return shop


class PreferenceInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    notifications_enabled = StrictBoolean()


class PreferenceSerializer(serializers.Serializer):
    shop_id = serializers.UUIDField(read_only=True)
    user_id = serializers.UUIDField(read_only=True)
    notifications_enabled = serializers.BooleanField(read_only=True)


def preference_receipt(shop, user, enabled):
    return json_value(PreferenceSerializer({"shop_id": shop.id, "user_id": user.id,
                                           "notifications_enabled": enabled}).data)


class StockNotificationPreferenceViewSet(viewsets.GenericViewSet):
    serializer_class = PreferenceSerializer
    queryset = StockNotificationPreference.objects.none()

    @extend_schema(responses=PreferenceSerializer)
    @transaction.atomic
    def retrieve(self, request, *args, **kwargs):
        shop = notification_context(request, self.kwargs["shop_id"])
        preference = StockNotificationPreference.objects.filter(shop=shop, user=request.user).first()
        return Response(preference_receipt(shop, request.user, preference.notifications_enabled if preference else True))

    @extend_schema(request=PreferenceInput, responses=PreferenceSerializer)
    @transaction.atomic
    def partial_update(self, request, *args, **kwargs):
        shop = notification_context(request, self.kwargs["shop_id"])
        serializer = PreferenceInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        canonical = json_value(data)
        original = original_receipt(data["operation_id"], shop, request.user,
                                    "stock_notification_preference_changed", canonical)
        if original is not None:
            return Response(original)
        preference, _ = StockNotificationPreference.objects.get_or_create(shop=shop, user=request.user)
        before = preference.notifications_enabled
        preference.notifications_enabled = data["notifications_enabled"]
        preference.save(update_fields=["notifications_enabled", "updated_at"])
        receipt = preference_receipt(shop, request.user, preference.notifications_enabled)
        Activity.objects.create(id=data["operation_id"], shop=shop, actor=request.user,
                                action="stock_notification_preference_changed", target_id=preference.id,
                                reason="Préférence personnelle de notification",
                                details={"request": canonical, "before": before,
                                         "after": preference.notifications_enabled, "response": receipt})
        return Response(receipt)


class StockAlertEventSerializer(serializers.ModelSerializer):
    product_id = serializers.UUIDField(read_only=True)
    operation_id = serializers.UUIDField(read_only=True)
    product_name = serializers.CharField(source="product.name", read_only=True)
    available_milli = serializers.IntegerField(read_only=True)
    provisional = serializers.BooleanField(default=True, read_only=True)

    class Meta:
        model = StockAlertEvent
        fields = ["id", "product_id", "product_name", "operation_id", "episode", "available_milli",
                  "threshold_milli", "created_at", "provisional"]
        read_only_fields = fields


class StockAlertEventViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = StockAlertEventSerializer
    queryset = StockAlertEvent.objects.none()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        shop = notification_context(self.request, self.kwargs["shop_id"])
        if StockNotificationPreference.objects.filter(shop=shop, user=self.request.user, notifications_enabled=False).exists():
            return self.queryset
        return StockAlertEvent.objects.filter(product__shop=shop, product__is_active=True,
            product__stock_threshold_milli__isnull=False, product__stock_alert_state__is_low=True,
            episode=F("product__stock_alert_state__episode")
        ).select_related("product").order_by("created_at", "id")

    @extend_schema(responses=StockAlertEventSerializer(many=True))
    @transaction.atomic
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)
