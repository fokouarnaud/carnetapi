from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from identity.services.access import accessible_shops
from identity.services.device_services import revoke_device, verify_access
from common.services.operations import require_manager
from identity.api.discount_policy import ChangeDiscountPolicySerializer, DiscountPolicySerializer, change_discount_policy, policy_response
from identity.models import Shop
from identity.api.shop_creation import CreateShopInput, create_shop
from identity.api.serializers import (
    AccessLeaseSerializer, RevokeDeviceResultSerializer, RevokeDeviceSerializer,
    ShopSerializer, VerifyAccessRequestSerializer,
)


class ShopViewSet(mixins.CreateModelMixin, viewsets.ReadOnlyModelViewSet):
    """Current server membership is checked before counting, paging or retrieval."""

    serializer_class = ShopSerializer
    queryset = Shop.objects.none()

    @extend_schema(request=CreateShopInput, responses={200: ShopSerializer, 201: ShopSerializer})
    def create(self, request, *args, **kwargs):
        serializer = CreateShopInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = create_shop(request.user, request.device_session, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)

    @extend_schema(request=ChangeDiscountPolicySerializer, responses=DiscountPolicySerializer)
    @action(detail=True, methods=["get", "post"], url_path="discount-policy")
    def discount_policy(self, request, pk=None):
        shop = self.get_object()
        if request.method == "GET":
            return Response(policy_response(shop))
        require_manager(shop, request.user)
        serializer = ChangeDiscountPolicySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(change_discount_policy(
            shop.id, request.user, request.device_session.installation, request.device_session.id,
            serializer.validated_data,
        ))

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return Shop.objects.none()
        if not hasattr(self.request, "device_session"):
            return Shop.objects.none()
        return accessible_shops(user, self.request.device_session.installation)

    @extend_schema(request=VerifyAccessRequestSerializer, responses=AccessLeaseSerializer)
    @action(detail=True, methods=["post"], url_path="verify-access")
    def verify_device_access(self, request, pk=None):
        shop = self.get_object()
        result = verify_access(shop, request.device_session.installation, request.user, request.device_session.id)
        return Response(AccessLeaseSerializer(result).data)

    @extend_schema(request=RevokeDeviceSerializer, responses=RevokeDeviceResultSerializer)
    @action(detail=True, methods=["post"], url_path="revoke-device")
    def revoke_device_access(self, request, pk=None):
        shop = self.get_object()
        serializer = RevokeDeviceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = revoke_device(shop, request.user, session_id=request.device_session.id,
                               actor_installation=request.device_session.installation, **serializer.validated_data)
        return Response(RevokeDeviceResultSerializer(result).data)
