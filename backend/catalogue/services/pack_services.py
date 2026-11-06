import json

from django.db import transaction
from django.utils.translation import gettext_lazy
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.renderers import JSONRenderer

from identity.services.access import accessible_shops, effective_permissions
from identity.services.authentication import require_live_session
from catalogue.api.catalogue_serializers import PackSerializer
from common.services.errors import OperationConflict
from common.models import Activity
from catalogue.models import Product, ProductPack
from catalogue.api.barcodes import register_created_code
from catalogue.api.catalogue_queries import with_codes
from common.services.operations import current_shop, json_value, original_receipt, require_manager


class ArchivedProductConflict(APIException):
    status_code = 409
    default_detail = gettext_lazy("Ce produit est archivé.")
    default_code = "product_archived"


@transaction.atomic
def create_pack(shop_id, product_id, actor, installation, validated_data, session_id):
    require_live_session(actor, session_id, installation)
    shop = get_object_or_404(accessible_shops(actor, installation), id=shop_id)
    product = get_object_or_404(Product, shop=shop, id=product_id)
    role, _ = effective_permissions(shop, actor)
    if role != "manager":
        raise PermissionDenied(gettext_lazy("Création de conditionnement réservée au responsable."))
    data = dict(validated_data)
    canonical = json.loads(JSONRenderer().render(data))
    canonical["product_id"] = str(product.id)
    if canonical.get("barcode") is None:
        canonical.pop("barcode", None)
    operation_id = data.pop("operation_id")
    original = original_receipt(operation_id, shop, actor, "pack_created", canonical)
    if original is not None:
        return original, False
    if ProductPack.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    if not product.is_active:
        raise ArchivedProductConflict()
    if product.unit_kind == Product.UnitKind.PIECE and data["base_quantity_milli"] % 1000:
        raise ValidationError({"base_quantity_milli": gettext_lazy("Le conditionnement doit contenir un nombre entier de pièces.")})
    code = data.pop("barcode")
    pack = ProductPack.objects.create(product=product, **data)
    register_created_code(shop, product, pack, code)
    receipt = json.loads(JSONRenderer().render(PackSerializer(pack).data))
    Activity.objects.create(
        id=operation_id, shop=shop, actor=actor, action="pack_created", target_id=pack.id,
        reason="Création du conditionnement", details={"request": canonical, "response": receipt},
    )
    return receipt, True


class PackVersionConflict(APIException):
    status_code = 409
    default_detail = gettext_lazy("Le conditionnement a changé. Relire sa version.")
    default_code = "pack_version_conflict"


@transaction.atomic
def update_pack(shop_id, product_id, pack_id, actor, installation, validated_data, session_id):
    require_live_session(actor, session_id, installation)
    shop = current_shop(shop_id, actor, installation)
    require_manager(shop, actor)
    product = get_object_or_404(Product, shop=shop, id=product_id)
    pack = get_object_or_404(with_codes(ProductPack.objects.filter(product=product), pack=True), id=pack_id)
    data = dict(validated_data)
    canonical = json_value({**data, "product_id": product.id, "pack_id": pack.id})
    operation_id = data.pop("operation_id")
    original = original_receipt(operation_id, shop, actor, "pack_updated", canonical)
    if original is not None:
        return original
    if {"sale_price_fcfa", "base_quantity_milli"} & data.keys():
        from catalogue.services.pack_price_conflicts import ensure_pack_price_uncontested
        ensure_pack_price_uncontested(pack.id)
    if pack.version != data.pop("base_version"):
        raise PackVersionConflict()
    reason = data.pop("reason")
    factor = data.get("base_quantity_milli", pack.base_quantity_milli)
    if product.unit_kind == Product.UnitKind.PIECE and factor % 1000:
        raise ValidationError({"base_quantity_milli": gettext_lazy("Le conditionnement doit contenir un nombre entier de pièces.")})
    before = json_value(PackSerializer(pack).data)
    for name, value in data.items():
        setattr(pack, name, value)
    pack.version += 1
    pack.save(update_fields=[*data, "version", "updated_at"])
    # Sale lines already validate this shared catalogue version before pricing.
    # Advance it so a cached pack cannot silently acquire a new factor or price.
    product.version += 1
    product.save(update_fields=["version", "updated_at"])
    pack.product = product
    receipt = json_value(PackSerializer(pack).data)
    Activity.objects.create(id=operation_id, shop=shop, actor=actor, action="pack_updated", target_id=pack.id,
                            reason=reason, details={"request": canonical, "before": before,
                                                    "after": receipt, "response": receipt})
    return receipt
