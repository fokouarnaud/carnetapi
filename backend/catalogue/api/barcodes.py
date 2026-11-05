"""Shop-scoped barcode assignment and exact lookup of confirmed selling formats."""
import uuid
from django.utils.translation import gettext_lazy
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from identity.services.authentication import require_live_session
from common.api.fields import ExactIntegerField, StrictSerializer
from catalogue.api.catalogue_serializers import CodeField
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from common.models import Activity
from catalogue.models import Barcode, Product, ProductPack


class AssignmentInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    base_version = ExactIntegerField(min_value=0, max_value=9223372036854775806)
    code = CodeField(allow_null=True)
    reason = serializers.CharField(max_length=2000, allow_blank=False)


class LookupInput(StrictSerializer):
    code = CodeField()


class BarcodeSerializer(serializers.ModelSerializer):
    shop_id = serializers.UUIDField(read_only=True)
    product_id = serializers.UUIDField(read_only=True)
    pack_id = serializers.UUIDField(read_only=True, allow_null=True)

    class Meta:
        model = Barcode
        fields = ["id", "shop_id", "product_id", "pack_id", "code", "version", "created_at", "updated_at"]
        read_only_fields = fields


class SellingFormatSerializer(serializers.Serializer):
    code = serializers.CharField(read_only=True)
    product_id = serializers.UUIDField(read_only=True)
    product_name = serializers.CharField(read_only=True)
    product_version = serializers.IntegerField(read_only=True)
    pack_id = serializers.UUIDField(read_only=True, allow_null=True)
    format_name = serializers.CharField(read_only=True)
    base_quantity_milli = serializers.IntegerField(read_only=True)
    sale_price_fcfa = serializers.IntegerField(read_only=True)
    barcode_version = serializers.IntegerField(read_only=True)


class BarcodeVersionConflict(APIException):
    status_code = 409
    default_detail = gettext_lazy("L'affectation du code a changé. Relire sa version.")
    default_code = "barcode_version_conflict"


class BarcodeConflict(APIException):
    status_code = 409
    default_code = "barcode_conflict"

    def __init__(self, existing):
        super().__init__({"code": "barcode_conflict", "conflicting_product_id": str(existing.product_id),
                          "conflicting_pack_id": str(existing.pack_id) if existing.pack_id else None})
        # DRF stringifies exception leaves, but an absent pack remains JSON null.
        if existing.pack_id is None:
            self.detail["conflicting_pack_id"] = None
        if (existing.product.shop_id != existing.shop_id
                or (existing.pack_id is not None and existing.pack.product_id != existing.product_id)):
            self.detail["conflicting_product_id"] = None
            self.detail["conflicting_pack_id"] = None


def barcode_context(request, shop_id):
    session = request.device_session
    require_live_session(request.user, session.id, session.installation)
    return current_shop(shop_id, request.user, session.installation)


class BarcodeAssignmentViewSet(viewsets.GenericViewSet):
    queryset = Barcode.objects.none()
    serializer_class = BarcodeSerializer

    @extend_schema(request=AssignmentInput, responses=BarcodeSerializer)
    @transaction.atomic
    def partial_update(self, request, *args, **kwargs):
        shop = barcode_context(request, self.kwargs["shop_id"])
        require_manager(shop, request.user)
        product = get_object_or_404(Product, shop=shop, id=self.kwargs["product_id"])
        pack = None
        if self.kwargs.get("pack_id") is not None:
            pack = get_object_or_404(ProductPack, product=product, id=self.kwargs["pack_id"])
        serializer = AssignmentInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        canonical = json_value({**data, "product_id": product.id, "pack_id": pack.id if pack else None})
        original = original_receipt(data["operation_id"], shop, request.user, "barcode_assigned", canonical)
        if original is not None:
            return Response(original)
        registry = Barcode.objects.filter(shop=shop, product=product, pack=pack).first()
        before = json_value(BarcodeSerializer(registry).data) if registry else {"code": None, "version": 0}
        if data["base_version"] != (registry.version if registry else 0):
            raise BarcodeVersionConflict()
        if data["code"] is not None and (not product.is_active or (pack is not None and not pack.is_active)):
            raise BarcodeVersionConflict("Cette unité de vente est archivée.")
        collision_query = Barcode.objects.filter(shop=shop, code=data["code"])
        if registry is not None:
            collision_query = collision_query.exclude(id=registry.id)
        if data["code"] is not None:
            collision = collision_query.first()
            if collision is not None:
                raise BarcodeConflict(collision)
        try:
            with transaction.atomic():
                if registry is None:
                    target = pack.id if pack else product.id
                    registry = Barcode.objects.create(id=uuid.uuid5(target, "barcode-registration"),
                        shop=shop, product=product, pack=pack, code=data["code"])
                else:
                    registry.code = data["code"]
                    registry.version += 1
                    registry.save(update_fields=["code", "version", "updated_at"])
        except IntegrityError:
            collision = collision_query.first() if data["code"] is not None else None
            if collision is None:
                raise
            raise BarcodeConflict(collision)
        receipt = json_value(BarcodeSerializer(registry).data)
        Activity.objects.create(id=data["operation_id"], shop=shop, actor=request.user, action="barcode_assigned",
            target_id=pack.id if pack else product.id, reason=data["reason"],
            details={"request": canonical, "before": before, "after": receipt, "response": receipt})
        return Response(receipt)


class BarcodeLookupViewSet(viewsets.GenericViewSet):
    queryset = Barcode.objects.none()
    serializer_class = SellingFormatSerializer

    @extend_schema(parameters=[LookupInput], responses=SellingFormatSerializer)
    @transaction.atomic
    def retrieve(self, request, *args, **kwargs):
        shop = barcode_context(request, self.kwargs["shop_id"])
        serializer = LookupInput(data=request.query_params.dict())
        serializer.is_valid(raise_exception=True)
        record = get_object_or_404(Barcode.objects.filter(shop=shop, product__shop=shop, product__is_active=True).filter(
            Q(pack__isnull=True) | Q(pack__is_active=True, pack__product_id=F("product_id"))
        ).select_related("product", "pack"), code=serializer.validated_data["code"])
        product, pack = record.product, record.pack
        return Response(SellingFormatSerializer({"code": record.code, "product_id": product.id,
            "product_name": product.name, "product_version": product.version, "pack_id": record.pack_id,
            "format_name": pack.name if pack else product.unit_label,
            "base_quantity_milli": pack.base_quantity_milli if pack else 1000,
            "sale_price_fcfa": pack.sale_price_fcfa if pack else product.sale_price_fcfa,
            "barcode_version": record.version}).data)


def register_created_code(shop, product, pack, code):
    """Claim an optional code within the caller's creation transaction."""
    target = pack if pack is not None else product
    target.barcode = code
    target.barcode_version = 0
    if code is None:
        return
    collision = Barcode.objects.filter(shop=shop, code=code).first()
    if collision is not None:
        raise BarcodeConflict(collision)
    try:
        with transaction.atomic():
            Barcode.objects.create(id=uuid.uuid5(target.id, "barcode-registration"),
                                   shop=shop, product=product, pack=pack, code=code)
    except IntegrityError:
        collision = Barcode.objects.filter(shop=shop, code=code).first()
        if collision is None:
            raise
        raise BarcodeConflict(collision)
    target.barcode_version = 1
