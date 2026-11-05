"""Observe final stock inside its business transaction, never while a line is unfinished."""
import uuid
from django.db import transaction
from rest_framework.exceptions import ValidationError
from catalogue.models import Product
from stock.models import StockAlertEvent, StockAlertState
from stock.services.stock_queries import with_stock


@transaction.atomic
def observe_stock(product_ids, operation):
    ids = set(product_ids)
    products = list(with_stock(Product.objects.filter(id__in=ids, shop_id=operation.shop_id)).order_by("id"))
    if {product.id for product in products} != ids:
        raise ValidationError("Produit étranger à l'opération de stock.")
    created = []
    for product in products:
        # Idempotent callers normally return their receipt before reaching this seam.
        if StockAlertEvent.objects.filter(product=product, operation=operation).exists():
            continue
        state, _ = StockAlertState.objects.get_or_create(product=product)
        low = product.is_active and product.stock_low
        if low and not state.is_low:
            state.episode += 1
            created.append(StockAlertEvent.objects.create(
                id=uuid.uuid5(product.id, f"stock-alert:{state.episode}"),
                product=product, operation=operation, episode=state.episode,
                available_milli=int(product.stock_available_milli), threshold_milli=product.stock_threshold_milli,
            ))
        if state.is_low != low:
            state.is_low = low
            state.save(update_fields=["is_low", "episode", "updated_at"])
    return created
