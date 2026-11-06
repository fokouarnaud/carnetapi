from decimal import Decimal

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from common.api.fields import ExactIntegerField, StrictBoolean, StrictSerializer
from catalogue.models import Product, ProductPack, ProductPhoto
from catalogue.api.photo_metadata import PhotoMetadataSerializer, photo_metadata
from catalogue.api.price_conflicts import PublicPriceConflictSerializer,public_price_conflict
from catalogue.api.pack_price_conflicts import public_pack_price_conflict


class CodeField(serializers.CharField):
    """Preserve opaque code text and leading zeros without numeric coercion."""
    def __init__(self, **kwargs):
        super().__init__(max_length=200, trim_whitespace=False, allow_blank=False, **kwargs)

    def to_internal_value(self, value):
        if not isinstance(value, str):
            self.fail("invalid")
        return super().to_internal_value(value)


class UpdateProductSerializer(StrictSerializer):
    category_id = serializers.UUIDField(required=False, allow_null=True)
    operation_id = serializers.UUIDField()
    base_version = ExactIntegerField(min_value=1, max_value=9223372036854775806)
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    name = serializers.CharField(max_length=150, allow_blank=False, required=False)
    sale_price_fcfa = ExactIntegerField(min_value=0, max_value=9223372036854775807, required=False)
    purchase_price_fcfa = ExactIntegerField(min_value=0, max_value=9223372036854775807, required=False, allow_null=True)
    stock_threshold_milli = ExactIntegerField(min_value=0, max_value=9223372036854775807, required=False, allow_null=True)
    is_active = StrictBoolean(required=False)

    def validate(self, data):
        if not set(data).intersection({"name", "sale_price_fcfa", "purchase_price_fcfa", "is_active", "category_id", "stock_threshold_milli"}):
            raise serializers.ValidationError("Aucun changement de catalogue proposé.")
        return data


class OpeningStockSerializer(StrictSerializer):
    quantity_milli = ExactIntegerField(min_value=0, max_value=9223372036854775807)
    unit_cost_fcfa = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=0,
        max_value=Decimal("9223372036854.775807"), allow_null=True,
    )
    occurred_at = serializers.DateTimeField()
    reason = serializers.CharField(max_length=2000, allow_blank=False, trim_whitespace=True)

    def to_internal_value(self, data):
        if isinstance(data, dict) and data.get("unit_cost_fcfa") is not None:
            if type(data["unit_cost_fcfa"]) not in (str, int):
                raise serializers.ValidationError({"unit_cost_fcfa": "Utiliser une chaîne décimale exacte."})
        return super().to_internal_value(data)

    def validate_occurred_at(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("Le stock initial ne peut pas être daté dans le futur.")
        return value


class CreateProductSerializer(StrictSerializer):
    barcode = CodeField(default=None, allow_null=True)
    category_id = serializers.UUIDField(default=None, allow_null=True)
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    name = serializers.CharField(max_length=150, trim_whitespace=True, allow_blank=False)
    unit_kind = serializers.ChoiceField(choices=Product.UnitKind.choices)
    unit_label = serializers.CharField(max_length=40, trim_whitespace=True, allow_blank=False)
    sale_price_fcfa = ExactIntegerField(min_value=0, max_value=9223372036854775807)
    purchase_price_fcfa = ExactIntegerField(min_value=0, max_value=9223372036854775807, allow_null=True, default=None)
    stock_threshold_milli = ExactIntegerField(min_value=0, max_value=9223372036854775807, allow_null=True, default=None)
    opening = OpeningStockSerializer(allow_null=True, default=None)

    def validate(self, data):
        threshold = data["stock_threshold_milli"]
        if threshold is not None and data["unit_kind"] == Product.UnitKind.PIECE and threshold % 1000:
            raise serializers.ValidationError({"stock_threshold_milli": _("Une pièce doit avoir une quantité entière.")})
        opening = data["opening"]
        if opening and data["unit_kind"] == Product.UnitKind.PIECE and opening["quantity_milli"] % 1000:
            raise serializers.ValidationError({"opening": _("Une pièce doit avoir une quantité entière.")})
        return data


class PublicProductSerializer(serializers.ModelSerializer):
    price_conflict = serializers.SerializerMethodField()
    photo = serializers.SerializerMethodField()
    barcode = serializers.CharField(read_only=True, default=None, allow_null=True)
    barcode_version = serializers.IntegerField(read_only=True, default=0)
    category_id = serializers.UUIDField(read_only=True, allow_null=True)
    stock_validated_milli = serializers.IntegerField(read_only=True)
    stock_reserved_milli = serializers.IntegerField(default=0, read_only=True)
    stock_pending_reserved_milli = serializers.IntegerField(default=0, read_only=True)
    stock_rejected_reserved_milli = serializers.IntegerField(default=0, read_only=True)
    stock_available_milli = serializers.SerializerMethodField()
    is_stock_low = serializers.SerializerMethodField()
    stock_provisional = serializers.BooleanField(default=True, read_only=True)
    stock_as_of = serializers.SerializerMethodField()

    @extend_schema_field(PublicPriceConflictSerializer(allow_null=True))
    def get_price_conflict(self,product):
        return public_price_conflict(product)

    @extend_schema_field(PhotoMetadataSerializer)
    def get_photo(self, product):
        try:
            photo = product.photo_record
        except ProductPhoto.DoesNotExist:
            photo = None
        return PhotoMetadataSerializer(photo_metadata(photo, product.id)).data

    def get_stock_available_milli(self, product) -> int:
        return int(product.stock_validated_milli) - int(getattr(product, "stock_reserved_milli", 0))

    def get_is_stock_low(self, product) -> bool:
        return (product.is_active and product.stock_threshold_milli is not None
                and self.get_stock_available_milli(product) <= product.stock_threshold_milli)

    def get_stock_as_of(self, product) -> str:
        return self.context.get("stock_as_of", timezone.now()).isoformat()

    class Meta:
        model = Product
        fields = ["id", "shop_id", "category_id", "name", "unit_kind", "unit_label", "sale_price_fcfa",
                  "is_active", "version", "price_conflict", "photo", "barcode", "barcode_version", "stock_threshold_milli", "stock_validated_milli", "stock_reserved_milli", "stock_pending_reserved_milli", "stock_rejected_reserved_milli", "stock_available_milli",
                  "is_stock_low", "stock_provisional", "stock_as_of", "created_at", "updated_at"]
        read_only_fields = fields


class CostProductSerializer(PublicProductSerializer):
    class Meta(PublicProductSerializer.Meta):
        fields = PublicProductSerializer.Meta.fields + ["purchase_price_fcfa"]
        read_only_fields = fields


class CreatePackSerializer(StrictSerializer):
    barcode = CodeField(default=None, allow_null=True)
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    name = serializers.CharField(max_length=80, trim_whitespace=True, allow_blank=False)
    base_quantity_milli = ExactIntegerField(min_value=1, max_value=9223372036854775807)
    sale_price_fcfa = ExactIntegerField(min_value=0, max_value=9223372036854775807)


class UpdatePackSerializer(StrictSerializer):
    operation_id = serializers.UUIDField()
    base_version = ExactIntegerField(min_value=1, max_value=9223372036854775806)
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    name = serializers.CharField(max_length=80, allow_blank=False, required=False)
    base_quantity_milli = ExactIntegerField(min_value=1, max_value=9223372036854775807, required=False)
    sale_price_fcfa = ExactIntegerField(min_value=0, max_value=9223372036854775807, required=False)
    is_active = StrictBoolean(required=False)

    def validate(self, data):
        if not set(data).intersection({"name", "base_quantity_milli", "sale_price_fcfa", "is_active"}):
            raise serializers.ValidationError(_("Aucun changement de conditionnement proposé."))
        return data


class PackSerializer(serializers.ModelSerializer):
    price_conflict = serializers.SerializerMethodField()
    product_id = serializers.UUIDField(read_only=True)
    product_version = serializers.IntegerField(source="product.version", read_only=True)
    product_updated_at = serializers.DateTimeField(source="product.updated_at", read_only=True)
    barcode = serializers.CharField(read_only=True, default=None, allow_null=True)
    barcode_version = serializers.IntegerField(read_only=True, default=0)

    @extend_schema_field(PublicPriceConflictSerializer(allow_null=True))
    def get_price_conflict(self, pack):
        return public_pack_price_conflict(pack)

    class Meta:
        model = ProductPack
        fields = ["id", "product_id", "version", "product_version", "product_updated_at", "name", "base_quantity_milli", "sale_price_fcfa", "is_active", "barcode", "barcode_version", "price_conflict", "created_at", "updated_at"]
        read_only_fields = fields
