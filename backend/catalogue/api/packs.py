from django.db import transaction
from django.utils.translation import gettext_lazy
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from identity.services.access import accessible_shops, effective_permissions
from identity.services.authentication import require_live_session
from catalogue.api.catalogue_serializers import CreatePackSerializer, PackSerializer, UpdatePackSerializer
from common.models import Activity
from catalogue.models import Product, ProductPack
from common.services.operations import require_manager
from catalogue.services.pack_services import create_pack, update_pack
from catalogue.api.catalogue_queries import NameSearchInput, filter_by_name, with_codes
from catalogue.api.pack_price_conflicts import with_pack_price_conflict_metadata


class PackHistorySerializer(serializers.ModelSerializer):
    actor_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Activity
        fields = ["id", "actor_id", "action", "reason", "details", "created_at"]
        read_only_fields = fields


class PackViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = ProductPack.objects.none()
    serializer_class = PackSerializer

    def get_product(self):
        require_live_session(self.request.user, self.request.device_session.id, self.request.device_session.installation)
        shop = get_object_or_404(
            accessible_shops(self.request.user, self.request.device_session.installation),
            id=self.kwargs["shop_id"],
        )
        return get_object_or_404(Product.objects.select_related("shop").defer("purchase_price_fcfa"), shop=shop, id=self.kwargs["product_id"])

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ProductPack.objects.none()
        queryset = with_pack_price_conflict_metadata(ProductPack.objects.filter(product=self.get_product())
            .select_related("product").defer("product__purchase_price_fcfa"))
        return filter_by_name(with_codes(queryset, pack=True),
                              self.request.query_params).order_by("created_at", "id")

    @extend_schema(parameters=[NameSearchInput], responses=PackSerializer(many=True))
    @transaction.atomic
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(request=CreatePackSerializer, responses={200: PackSerializer, 201: PackSerializer})
    def create(self, request, *args, **kwargs):
        product = self.get_product()
        role, _ = effective_permissions(product.shop, request.user)
        if role != "manager":
            raise PermissionDenied(gettext_lazy("Création de conditionnement réservée au responsable."))
        serializer = CreatePackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = create_pack(
            self.kwargs["shop_id"], product.id, request.user,
            request.device_session.installation, serializer.validated_data, request.device_session.id,
        )
        return Response(receipt, status=201 if created else 200)


    @extend_schema(responses=PackSerializer)
    @transaction.atomic
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(request=UpdatePackSerializer, responses=PackSerializer)
    def partial_update(self, request, *args, **kwargs):
        product = self.get_product()
        role, _ = effective_permissions(product.shop, request.user)
        if role != "manager":
            raise PermissionDenied(gettext_lazy("Modification de conditionnement réservée au responsable."))
        serializer = UpdatePackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt = update_pack(self.kwargs["shop_id"], product.id, self.kwargs["pk"], request.user,
                              request.device_session.installation, serializer.validated_data, request.device_session.id)
        return Response(receipt)


    @extend_schema(responses=PackHistorySerializer(many=True))
    @transaction.atomic
    def history(self, request, *args, **kwargs):
        pack = self.get_object()
        require_manager(pack.product.shop, request.user)
        events = Activity.objects.filter(shop_id=pack.product.shop_id, target_id=pack.id,
            action__in=["pack_created", "pack_updated", "catalogue_pack_price_conflict_recorded",
                "catalogue_pack_price_resolved"]).order_by("created_at", "id")
        page = self.paginate_queryset(events)
        return self.get_paginated_response(PackHistorySerializer(page, many=True).data)
