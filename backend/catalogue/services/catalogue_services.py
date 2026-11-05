"""Catalogue changes do not create stock or revalue past movements."""
from django.utils.translation import gettext_lazy as _
from stock.services.stock_alert_services import observe_stock
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import APIException, ValidationError

from catalogue.api.catalogue_serializers import CostProductSerializer
from identity.services.authentication import require_live_session
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from common.models import Activity
from catalogue.models import Category, Product
from stock.models import StockMovement
from catalogue.api.barcodes import register_created_code
from common.services.errors import OperationConflict
from stock.services.stock_queries import with_stock
from catalogue.api.catalogue_queries import with_codes


class ProductVersionConflict(APIException):
    status_code = 409
    default_detail = _("Le produit a été modifié. Relire sa version avant correction.")
    default_code = "product_version_conflict"


@transaction.atomic
def update_product(shop_id, product_id, actor, installation, validated_data, session_id):
    require_live_session(actor, session_id, installation)
    shop = current_shop(shop_id, actor, installation)
    require_manager(shop, actor)
    product = get_object_or_404(with_codes(with_stock(Product.objects.filter(shop=shop))), id=product_id)
    data = dict(validated_data)
    canonical = json_value({**data, "product_id": product_id})
    operation_id = data.pop("operation_id")
    original = original_receipt(operation_id, shop, actor, "product_updated", canonical)
    if original is not None:
        return original
    if 'sale_price_fcfa' in data:
        from catalogue.services.price_conflicts import ensure_price_uncontested
        ensure_price_uncontested(product.id)
    if product.version != data.pop("base_version"):
        raise ProductVersionConflict()
    reason = data.pop("reason")
    if data.get("category_id") is not None:
        get_object_or_404(Category, shop=shop, id=data["category_id"])
    threshold = data.get("stock_threshold_milli")
    if threshold is not None and product.unit_kind == Product.UnitKind.PIECE and threshold % 1000:
        raise ValidationError({"stock_threshold_milli": _("Une pièce doit avoir une quantité entière.")})
    before = json_value(CostProductSerializer(product).data)
    for name, value in data.items():
        setattr(product, name, value)
    product.version += 1
    product.save(update_fields=[*data, "version", "updated_at"])
    receipt = json_value(CostProductSerializer(product).data)
    event = Activity.objects.create(
        id=operation_id, shop=shop, actor=actor, action="product_updated", target_id=product.id, reason=reason,
        details={"request": canonical, "before": before, "after": receipt, "response": receipt},
    )
    observe_stock([product.id], event)
    return receipt


def product_request_value(validated_data):
    """Serialize an opening cost as exact decimal text, never through a JSON float."""
    data = dict(validated_data)
    if data.get("opening") is not None:
        opening = dict(data["opening"])
        if opening["unit_cost_fcfa"] is not None:
            opening["unit_cost_fcfa"] = str(opening["unit_cost_fcfa"])
        data["opening"] = opening
    return json_value(data)


def original_product_request(event):
    """Recover exact costs for old audit requests whose renderer used floats."""
    from decimal import Decimal
    previous = dict(event.details.get("request") or {})
    opening = previous.get("opening")
    if isinstance(opening, dict) and type(opening.get("unit_cost_fcfa")) in (float, int):
        movement = StockMovement.objects.filter(operation=event, kind="opening").first()
        if movement is not None and movement.unit_cost_micros is not None:
            opening = dict(opening)
            opening["unit_cost_fcfa"] = format(Decimal(movement.unit_cost_micros) / Decimal(1000000), ".6f")
            previous["opening"] = opening
    return previous


@transaction.atomic
def create_product(shop_id, actor, installation, validated_data, session_id):
    require_live_session(actor, session_id, installation)
    shop = current_shop(shop_id, actor, installation)
    require_manager(shop, actor)
    data = dict(validated_data)
    canonical = product_request_value(data)
    # A new optional category must not invalidate receipts issued before it existed.
    if canonical.get("category_id") is None:
        canonical.pop("category_id", None)
    if canonical.get("stock_threshold_milli") is None:
        canonical.pop("stock_threshold_milli", None)
    if canonical.get("barcode") is None:
        canonical.pop("barcode", None)
    operation_id = data.pop("operation_id")
    existing = Activity.objects.filter(id=operation_id).first()
    if existing:
        if (existing.shop_id != shop.id or existing.actor_id != actor.id
                or existing.action != "product_created" or original_product_request(existing) != canonical):
            raise OperationConflict()
        return existing.details["response"], False
    # Preserve legacy cost-aware receipts above, then reserve captured/review UUIDs.
    original = original_receipt(operation_id, shop, actor, "product_created", canonical)
    if original is not None:
        return original, False
    if Product.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    opening = data.pop("opening")
    if data["category_id"] is not None:
        get_object_or_404(Category, shop=shop, id=data["category_id"])
    code = data.pop("barcode")
    product = Product.objects.create(shop=shop, **data)
    register_created_code(shop, product, None, code)
    audit = Activity.objects.create(
        id=operation_id, shop=shop, actor=actor, action="product_created", target_id=product.id,
        reason=opening["reason"] if opening else "Création du produit",
        details={"request": canonical, "opening": canonical["opening"]},
    )
    if opening:
        movement_data = dict(opening)
        cost = movement_data.pop("unit_cost_fcfa")
        StockMovement.objects.create(
            product=product, actor=actor, operation=audit, kind="opening",
            unit_cost_micros=int(cost * 1000000) if cost is not None else None, **movement_data,
        )
    product.stock_validated_milli = opening["quantity_milli"] if opening else 0
    receipt = json_value(CostProductSerializer(product).data)
    audit.details["response"] = receipt
    audit.save(update_fields=["details", "updated_at"])
    observe_stock([product.id], audit)
    return receipt, True
