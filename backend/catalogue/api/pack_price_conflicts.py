"""Scoped pack-price review, bounded proposals and public conflict indicators."""
from django.db import transaction
from django.db.models import BigIntegerField, Count, OuterRef, Prefetch, Subquery, UUIDField
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.response import Response

from catalogue.api.price_conflicts import (
    PriceProposalSerializer, ProductPriceConflictSerializer,
    RecordPriceConflictInput, ResolvePriceConflictInput, PriceOperationSnapshotSerializer,
)
from catalogue.models import PackPriceConflict, PackPriceProposal, Product, ProductPack
from common.services.operations import current_shop, require_manager
from common.services.pagination import BoundedPagination
from identity.services.authentication import require_live_session


def with_pack_price_conflict_metadata(queryset):
    """Read the three public facts within the authorized pack's SQL page."""
    active = PackPriceConflict.objects.filter(
        pack_id=OuterRef("pk"), product_id=OuterRef("product_id"),
        shop_id=OuterRef("product__shop_id"), state="open",
    ).order_by("created_at", "id")
    return queryset.annotate(
        public_pack_price_conflict_id=Subquery(active.values("id")[:1], output_field=UUIDField()),
        public_pack_price_conflict_common_price_fcfa=Subquery(
            active.values("common_price_fcfa")[:1], output_field=BigIntegerField()),
    )


def public_pack_price_conflict(pack):
    """Use page annotations or one bounded lookup for a native single receipt."""
    if hasattr(pack, "public_pack_price_conflict_id"):
        identifier = pack.public_pack_price_conflict_id
        price = pack.public_pack_price_conflict_common_price_fcfa
    else:
        row = PackPriceConflict.objects.filter(
            pack_id=pack.id, product_id=pack.product_id,
            shop_id=pack.product.shop_id, state="open",
        ).order_by("created_at", "id").values("id", "common_price_fcfa").first()
        identifier = row["id"] if row else None
        price = row["common_price_fcfa"] if row else None
    return None if identifier is None else {
        "id": str(identifier), "state": "open", "common_price_fcfa": price,
    }


class PackPriceConflictSerializer(ProductPriceConflictSerializer):
    pack_id = serializers.UUIDField()
    base_quantity_milli = serializers.IntegerField(min_value=1)
    pack_version = serializers.IntegerField(min_value=1)
    proposals = serializers.ListField(child=PriceProposalSerializer(), max_length=25)
    proposals_count = serializers.IntegerField(min_value=0)
    proposals_url = serializers.CharField()


class PackPriceConflictPreviewSerializer(PackPriceConflictSerializer):
    preview_token = serializers.CharField(max_length=512, allow_null=True)
    can_resolve = serializers.BooleanField()


class PackPriceConflictRecordReceiptSerializer(PackPriceConflictSerializer):
    recorded_proposal = PriceProposalSerializer(required=False)
    operation_snapshot = PriceOperationSnapshotSerializer(required=False)


class CreatedPackPriceConflictRecordReceiptSerializer(PackPriceConflictRecordReceiptSerializer):
    recorded_proposal = PriceProposalSerializer()
    operation_snapshot = PriceOperationSnapshotSerializer()


class PackPriceOperationReceiptSerializer(PackPriceConflictSerializer):
    operation_snapshot = PriceOperationSnapshotSerializer(required=False)


class PackPriceConflictViewSet(viewsets.GenericViewSet):
    serializer_class = PackPriceConflictSerializer
    pagination_class = BoundedPagination

    def context(self):
        request = self.request
        require_live_session(request.user, request.device_session.id, request.device_session.installation)
        shop = current_shop(self.kwargs["shop_id"], request.user, request.device_session.installation)
        require_manager(shop, request.user)
        product = get_object_or_404(Product.objects.only("id", "shop_id"), shop=shop, id=self.kwargs["product_id"])
        pack = get_object_or_404(ProductPack.objects.only("id", "product_id"), product=product, id=self.kwargs["pack_id"])
        return shop, product, pack

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return PackPriceConflict.objects.none()
        shop, product, pack = self.context()
        return PackPriceConflict.objects.filter(shop=shop, product=product, pack=pack).annotate(
            receipt_proposals_count=Count("proposals"),
        ).select_related("pack__product", "resolution").prefetch_related(
            Prefetch("proposals", queryset=PackPriceProposal.objects.order_by("created_at", "id")[:25],
                to_attr="receipt_proposals"),
        ).order_by("created_at", "id")

    @extend_schema(responses=PackPriceConflictSerializer(many=True))
    @transaction.atomic
    def list(self, request, *args, **kwargs):
        from catalogue.services.pack_price_conflicts import pack_price_conflict_state
        rows = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response([pack_price_conflict_state(row) for row in rows])

    @extend_schema(responses=PackPriceConflictSerializer)
    @transaction.atomic
    def retrieve(self, request, *args, **kwargs):
        from catalogue.services.pack_price_conflicts import pack_price_conflict_state
        return Response(pack_price_conflict_state(self.get_object()))

    @extend_schema(responses=PriceProposalSerializer(many=True))
    @transaction.atomic
    def proposals(self, request, *args, **kwargs):
        shop, product, pack = self.context()
        conflict = get_object_or_404(PackPriceConflict.objects.only("id"),
            shop=shop, product=product, pack=pack, id=self.kwargs["pk"])
        rows = PackPriceProposal.objects.filter(conflict=conflict).order_by("created_at", "id")
        return self.get_paginated_response(PriceProposalSerializer(self.paginate_queryset(rows), many=True).data)

    @extend_schema(request=RecordPriceConflictInput,
        responses={200: PackPriceConflictRecordReceiptSerializer, 201: CreatedPackPriceConflictRecordReceiptSerializer})
    def create(self, request, *args, **kwargs):
        from catalogue.services.pack_price_conflicts import record_pack_price_conflict
        self.context()
        incoming = RecordPriceConflictInput(data=request.data)
        incoming.is_valid(raise_exception=True)
        result, created = record_pack_price_conflict(
            self.kwargs["shop_id"], self.kwargs["product_id"], self.kwargs["pack_id"],
            request.user, request.device_session.installation, incoming.validated_data, request.device_session.id)
        return Response(result, status=201 if created else 200)

    @extend_schema(responses=PackPriceConflictPreviewSerializer)
    def preview(self, request, *args, **kwargs):
        from catalogue.services.pack_price_conflicts import preview_pack_price_conflict
        self.context()
        return Response(preview_pack_price_conflict(
            self.kwargs["shop_id"], self.kwargs["product_id"], self.kwargs["pack_id"], self.kwargs["pk"],
            request.user, request.device_session.installation, request.device_session.id))

    @extend_schema(request=ResolvePriceConflictInput, responses=PackPriceOperationReceiptSerializer)
    def resolve(self, request, *args, **kwargs):
        from catalogue.services.pack_price_conflicts import resolve_pack_price_conflict
        self.context()
        incoming = ResolvePriceConflictInput(data=request.data)
        incoming.is_valid(raise_exception=True)
        result, _created = resolve_pack_price_conflict(
            self.kwargs["shop_id"], self.kwargs["product_id"], self.kwargs["pack_id"], self.kwargs["pk"],
            request.user, request.device_session.installation, incoming.validated_data, request.device_session.id)
        return Response(result)
