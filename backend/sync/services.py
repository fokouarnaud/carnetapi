"""Sequence attribution for the shop change feed (G6)."""
from django.db import transaction
from django.db.models import F

from sync.models import ChangeFeedEntry, ShopChangeSequence


def record_change(shop, object_type, object_id, kind, actor=None):
    """Append a feed entry with the next shop sequence, in the caller's transaction."""
    with transaction.atomic():
        ShopChangeSequence.objects.get_or_create(shop=shop)
        ShopChangeSequence.objects.filter(shop=shop).update(last_seq=F("last_seq") + 1)
        seq = ShopChangeSequence.objects.values_list("last_seq", flat=True).get(shop=shop)
        return ChangeFeedEntry.objects.create(shop=shop, seq=seq, object_type=object_type,
                                              object_id=object_id, kind=kind, actor=actor)


# G7: deny by default. Sellers always receive shared reference data, and money
# operations only when they authored them; purchases, suppliers and costs never.
SHARED_PREFIXES = ("product_", "pack_", "category_", "barcode_", "catalogue_price_", "client_")
OWN_PREFIXES = ("sale_", "customer_payment_", "cash_")


DEBT_PREFIX = "customer_debt_"


def visible_to_seller(entry, user_id, can_read_debt=False):
    action = entry.object_type
    if action.startswith(SHARED_PREFIXES):
        return True
    if action.startswith(DEBT_PREFIX):  # G7: debts only with the (effective) debt-reading permission
        return can_read_debt
    return action.startswith(OWN_PREFIXES) and entry.actor_id == user_id
