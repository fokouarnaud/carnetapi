from django.shortcuts import get_object_or_404
from django.db import transaction
from identity.services.authentication import require_live_session
from common.services.operations import require_manager
from drf_spectacular.utils import PolymorphicProxySerializer, extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.response import Response

from identity.services.access import accessible_shops, effective_permissions
from common.models import Activity
from sales.models import Sale
from sales.api.sale_serializers import APIConflictSerializer, CreateSaleSerializer, DebtConfirmationSerializer, SaleSerializer
from sales.services.sale_services import create_sale
from common.services.origins import with_public_origin
from common.api.errors import OriginReferenceError


class SaleHistorySerializer(serializers.ModelSerializer):
    actor_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Activity
        fields = ["id", "actor_id", "action", "reason", "details", "created_at"]
        read_only_fields = fields


class SaleViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = Sale.objects.none()
    serializer_class = SaleSerializer

    def get_shop(self):
        return get_object_or_404(
            accessible_shops(self.request.user, self.request.device_session.installation), id=self.kwargs["shop_id"],
        )

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Sale.objects.none()
        shop = self.get_shop()
        _, permissions = effective_permissions(shop, self.request.user)
        sales = Sale.objects.filter(shop=shop)
        if not permissions["can_read_debt"]:
            sales = sales.filter(mode="cash", customer_credit__isnull=True)
        return with_public_origin(sales).select_related("debt", "debt__shop", "customer_credit", "cancellation").prefetch_related("lines", "payments").order_by("created_at", "id")

    @extend_schema(responses={200:SaleSerializer(many=True),409:OriginReferenceError})
    def list(self,request,*args,**kwargs):return super().list(request,*args,**kwargs)

    @extend_schema(responses={200:SaleSerializer,409:OriginReferenceError})
    def retrieve(self,request,*args,**kwargs):return super().retrieve(request,*args,**kwargs)

    @extend_schema(request=CreateSaleSerializer, responses={
        200: SaleSerializer, 201: SaleSerializer,
        409: PolymorphicProxySerializer(component_name="SaleConflictResponse",
                                       serializers=[APIConflictSerializer, DebtConfirmationSerializer],
                                       resource_type_field_name=None),
    })
    def create(self, request, *args, **kwargs):
        self.get_shop()
        serializer = CreateSaleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = create_sale(
            self.kwargs["shop_id"], request.user, request.device_session.installation, serializer.validated_data,
            session_id=request.device_session.id,
        )
        return Response(receipt, status=201 if created else 200)


    @extend_schema(responses=SaleHistorySerializer(many=True))
    @transaction.atomic
    def history(self, request, *args, **kwargs):
        require_live_session(request.user, request.device_session.id, request.device_session.installation)
        shop = self.get_shop()
        require_manager(shop, request.user)
        sale = get_object_or_404(Sale, shop=shop, id=self.kwargs["pk"])
        events = Activity.objects.filter(shop=shop, target_id=sale.id, action__in=["sale_created", "sale_cancelled"]).order_by("created_at", "id")
        page = self.paginate_queryset(events)
        return self.get_paginated_response(SaleHistorySerializer(page, many=True).data)
