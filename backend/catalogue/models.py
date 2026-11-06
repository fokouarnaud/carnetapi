
from django.db import models
from django.db.models.lookups import Exact, GreaterThan, LessThanOrEqual
from django.utils.translation import gettext_lazy as _
from common.model_base import UUIDRecord, _integer_columns
from catalogue.price_conflict_models import ProductPriceConflict, ProductPriceProposal, ProductPriceResolution
from catalogue.pack_price_conflict_models import PackPriceConflict, PackPriceProposal, PackPriceResolution


class Category(UUIDRecord):
    """A shop-local catalogue grouping independent of stock and money."""
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="categories")
    name = models.CharField(max_length=150)
    version = models.PositiveBigIntegerField(default=1)

    class Meta:
        db_table = "core_category"
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["shop", "created_at", "id"], name="core_catego_shop_id_d3067b_idx")]
        constraints = [models.CheckConstraint(condition=models.Q(version__gt=0), name="category_positive_version"),
                       models.CheckConstraint(condition=_integer_columns("version"), name="category_integral_version")]


class Product(UUIDRecord):
    class UnitKind(models.TextChoices):
        PIECE = "piece", _("Piece")
        WEIGHT = "weight", _("Weight")
        VOLUME = "volume", _("Volume")

    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="products")
    category = models.ForeignKey('catalogue.Category', on_delete=models.PROTECT, null=True, blank=True, related_name="products")
    name = models.CharField(max_length=150)
    unit_kind = models.CharField(max_length=10, choices=UnitKind.choices)
    unit_label = models.CharField(max_length=40)
    sale_price_fcfa = models.PositiveBigIntegerField()
    purchase_price_fcfa = models.PositiveBigIntegerField(null=True, blank=True)
    stock_threshold_milli = models.PositiveBigIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    version = models.PositiveBigIntegerField(default=1)

    class Meta:
        db_table = "core_product"
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["shop", "created_at", "id"], name="core_produc_shop_id_aeb8a2_idx")]
        constraints = [
            models.CheckConstraint(condition=_integer_columns("sale_price_fcfa")
                & (models.Q(purchase_price_fcfa__isnull=True) | _integer_columns("purchase_price_fcfa")),
                name="product_integer_prices"),
            models.CheckConstraint(condition=models.Q(version__gt=0), name="product_positive_version"),
            models.CheckConstraint(condition=_integer_columns("version"), name="product_integral_version"),
            models.CheckConstraint(condition=(models.Q(stock_threshold_milli__isnull=True)
                | (models.Q(stock_threshold_milli__gte=0) & _integer_columns("stock_threshold_milli"))),
                name="product_threshold_nonnegative_integer"),
            models.CheckConstraint(condition=(models.Q(stock_threshold_milli__isnull=True)
                | models.Q(unit_kind__in=["weight", "volume"])
                | (models.Q(unit_kind="piece") & Exact(models.F("stock_threshold_milli") % 1000, models.Value(0)))),
                name="product_threshold_unit_precision"),
        ]


class ProductPhoto(UUIDRecord):
    """The current normalized image is stored with SQLite for atomic replacement."""
    product = models.OneToOneField('catalogue.Product', on_delete=models.PROTECT, related_name="photo_record")
    data = models.BinaryField(null=True, blank=True)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    version = models.PositiveBigIntegerField(default=1)

    @property
    def size_bytes(self):
        return len(self.data) if self.data is not None else 0

    class Meta:
        db_table = "core_productphoto"
        constraints = [
            models.CheckConstraint(condition=models.Q(version__gt=0) & _integer_columns("version", "width", "height"), name="photo_integer_metadata"),
            models.CheckConstraint(condition=(models.Q(data__isnull=True, width=0, height=0)
                | (models.Q(data__isnull=False, width__gt=0, height__gt=0, width__lte=640, height__lte=640)
                   & Exact(models.Func(models.F("data"), function="typeof", output_field=models.CharField()), models.Value("blob"))
                   & GreaterThan(models.Func(models.F("data"), function="length", output_field=models.IntegerField()), models.Value(0))
                   & LessThanOrEqual(models.Func(models.F("data"), function="length", output_field=models.IntegerField()), models.Value(20000)))),
                name="photo_bounded_content"),
        ]


class ProductPack(UUIDRecord):
    """A named selling unit sharing its product's base stock."""

    product = models.ForeignKey('catalogue.Product', on_delete=models.PROTECT, related_name="packs")
    name = models.CharField(max_length=80)
    base_quantity_milli = models.PositiveBigIntegerField()
    sale_price_fcfa = models.PositiveBigIntegerField()
    is_active = models.BooleanField(default=True)
    version = models.PositiveBigIntegerField(default=1)

    class Meta:
        db_table = "core_productpack"
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["product", "created_at", "id"], name="core_produc_product_2ac09a_idx")]
        constraints = [models.CheckConstraint(condition=models.Q(base_quantity_milli__gt=0), name="pack_positive_quantity"),
                       models.CheckConstraint(condition=models.Q(version__gt=0) & _integer_columns("version"), name="pack_positive_integer_version"),
                       models.CheckConstraint(condition=_integer_columns("base_quantity_milli", "sale_price_fcfa"), name="pack_integer_values")]


class Barcode(UUIDRecord):
    """A confirmed shop-local code, versioned independently from sale tariffs."""
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="barcodes")
    product = models.ForeignKey('catalogue.Product', on_delete=models.PROTECT, related_name="barcodes")
    pack = models.OneToOneField('catalogue.ProductPack', on_delete=models.PROTECT, related_name="barcode_registration", null=True, blank=True)
    code = models.CharField(max_length=200, null=True, blank=True)
    version = models.PositiveBigIntegerField(default=1)

    class Meta:
        db_table = "core_barcode"
        constraints = [
            models.UniqueConstraint(fields=["shop", "code"], name="barcode_shop_code_once"),
            models.UniqueConstraint(fields=["product"], condition=models.Q(pack__isnull=True), name="barcode_unit_once"),
            models.CheckConstraint(condition=models.Q(code__isnull=True) | ~models.Q(code=""), name="barcode_nonempty_code"),
            models.CheckConstraint(condition=models.Q(code__isnull=True) | models.Q(
                LessThanOrEqual(models.Func(models.F("code"), function="length", output_field=models.IntegerField()), models.Value(200))),
                name="barcode_bounded_code"),
            models.CheckConstraint(condition=models.Q(version__gt=0) & _integer_columns("version"), name="barcode_positive_integer_version"),
        ]


class CatalogueImport(UUIDRecord):
    """A shop-scoped CSV source survives preview and has one atomic application."""
    kind = models.CharField(max_length=12, default="products", choices=[("products", _("Products")), ("customers", _("Customers")), ("suppliers", _("Suppliers"))])
    shop = models.ForeignKey('identity.Shop', on_delete=models.PROTECT, related_name="catalogue_imports")
    actor = models.ForeignKey('core.User', on_delete=models.PROTECT, related_name="catalogue_imports")
    installation = models.ForeignKey('identity.Installation', on_delete=models.PROTECT, related_name="catalogue_imports")
    source = models.BinaryField()
    source_sha256 = models.CharField(max_length=64)
    applied_operation = models.OneToOneField('core.Activity', on_delete=models.PROTECT, null=True, blank=True,
                                             related_name="catalogue_import")
    response = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "core_catalogueimport"
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["shop", "created_at", "id"], name="core_catalo_shop_id_48a255_idx")]
        constraints = [
            models.CheckConstraint(condition=models.Q(kind__in=["products", "customers", "suppliers"]), name="csv_import_supported_kind"),
            models.CheckConstraint(condition=(
                Exact(models.Func(models.F("source"), function="typeof", output_field=models.CharField()), models.Value("blob"))
                & GreaterThan(models.Func(models.F("source"), function="length", output_field=models.IntegerField()), models.Value(0))
                & LessThanOrEqual(models.Func(models.F("source"), function="length", output_field=models.IntegerField()), models.Value(1000000))),
                name="catalogue_import_bounded_source"),
            models.CheckConstraint(condition=(models.Q(applied_operation__isnull=True, response__isnull=True)
                | models.Q(applied_operation__isnull=False, response__isnull=False)), name="catalogue_import_receipt_pair"),
        ]
