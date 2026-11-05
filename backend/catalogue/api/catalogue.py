from django.utils.translation import gettext_lazy

from django.db import transaction
from django.utils import timezone
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from identity.services.access import accessible_shops, effective_permissions
from identity.services.authentication import require_live_session
from catalogue.api.catalogue_serializers import CostProductSerializer, CreateProductSerializer, PublicProductSerializer, UpdateProductSerializer
from catalogue.services.catalogue_services import create_product, update_product
from common.services.operations import require_manager
from common.models import Activity
from catalogue.models import Product
from catalogue.api.catalogue_queries import NameSearchInput, filter_by_name, with_codes
from catalogue.api.price_conflicts import with_price_conflict_metadata
from stock.services.stock_queries import with_stock
from backups.api.recovery_metadata import RecoveryPagination
from catalogue.api.recovery_serializers import RoleProductResponse,PublicRecoveryProductSerializer,CostRecoveryProductSerializer
from backups.services.recovery_status import recovery_status_for_shop


def product_response(*,detail=False):
    return RoleProductResponse(
        component_name="ProductRecoveryResponse" if detail else "ProductResponse",
        serializers=[PublicRecoveryProductSerializer,CostRecoveryProductSerializer] if detail else [PublicProductSerializer,CostProductSerializer],
        resource_type_field_name=None,
    )


class ProductHistorySerializer(serializers.ModelSerializer):
    actor_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Activity
        fields = ["id", "actor_id", "action", "reason", "details", "created_at"]


class ProductViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = Product.objects.none()
    serializer_class = PublicProductSerializer
    pagination_class = RecoveryPagination

    def get_shop(self):
        if not hasattr(self, "_shop"):
            self._shop = get_object_or_404(
                accessible_shops(self.request.user, self.request.device_session.installation),
                id=self.kwargs["shop_id"],
            )
        return self._shop

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if not hasattr(self, "_stock_as_of"):
            self._stock_as_of = timezone.now()
        context["stock_as_of"] = self._stock_as_of
        if self.action=="retrieve":
            context["restoration"]=recovery_status_for_shop(self.get_shop())
        return context

    def get_serializer_class(self):
        if getattr(self, "swagger_fake_view", False):
            return PublicProductSerializer
        _, rights = effective_permissions(self.get_shop(), self.request.user)
        if self.action=="retrieve":
            return CostRecoveryProductSerializer if rights["can_read_cost"] else PublicRecoveryProductSerializer
        return CostProductSerializer if rights["can_read_cost"] else PublicProductSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Product.objects.none()
        require_live_session(self.request.user, self.request.device_session.id, self.request.device_session.installation)
        products = with_price_conflict_metadata(with_codes(with_stock(Product.objects.filter(shop=self.get_shop())))).select_related("photo_record").order_by("created_at", "id")
        if self.action == "list":
            products = filter_by_name(products, self.request.query_params)
        if not issubclass(self.get_serializer_class(),CostProductSerializer):
            products = products.defer("purchase_price_fcfa")
        return products

    @extend_schema(parameters=[NameSearchInput], responses=product_response())
    @transaction.atomic
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(responses=product_response(detail=True))
    @transaction.atomic
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(request=UpdateProductSerializer, responses=CostProductSerializer)
    def partial_update(self, request, *args, **kwargs):
        self.get_object()
        require_manager(self.get_shop(), request.user)
        serializer = UpdateProductSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt = update_product(
            self.kwargs["shop_id"], self.kwargs["pk"], request.user,
            request.device_session.installation, serializer.validated_data,
            request.device_session.id,
        )
        return Response(receipt)

    @extend_schema(responses=ProductHistorySerializer(many=True))
    @transaction.atomic
    def history(self, request, *args, **kwargs):
        product = self.get_object()
        require_manager(self.get_shop(), request.user)
        events = Activity.objects.filter(
            shop_id=product.shop_id, target_id=product.id,
            action__in=["product_created", "product_updated", "product_photo_updated", "product_photo_removed",
                        "catalogue_price_conflict_recorded", "catalogue_price_resolved"],
        ).order_by("created_at", "id")
        page = self.paginate_queryset(events)
        return self.get_paginated_response(ProductHistorySerializer(page, many=True).data)

    @extend_schema(request=CreateProductSerializer, responses={200: CostProductSerializer, 201: CostProductSerializer})
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        require_live_session(request.user, request.device_session.id, request.device_session.installation)
        shop = self.get_shop()
        role, _ = effective_permissions(shop, request.user)
        if role != "manager":
            raise PermissionDenied(gettext_lazy("Création de produit réservée au responsable."))
        serializer = CreateProductSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = create_product(self.kwargs["shop_id"], request.user, request.device_session.installation,
                                          serializer.validated_data, request.device_session.id)
        return Response(receipt, status=201 if created else 200)
