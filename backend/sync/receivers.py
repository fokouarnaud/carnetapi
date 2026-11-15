"""Every journaled shop write appends one feed entry in the same transaction (E7)."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from common.models import Activity
from sync.services import record_change


@receiver(post_save, sender=Activity, dispatch_uid="sync_activity_change_feed")
def append_activity_to_feed(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        record_change(instance.shop, instance.action, instance.target_id, "upsert", actor=instance.actor)


@receiver(post_save, sender="stock.StockMovement", dispatch_uid="sync_stock_movement_change_feed")
def append_stock_effect_to_feed(sender, instance, created, raw=False, **kwargs):
    """G6/G7: other phones learn the current stock of every product a movement changes.

    The opening is skipped: it is written with the product creation, whose
    entry already serializes the current stock when the feed is read.
    """
    if created and not raw and instance.kind != "opening":
        record_change(instance.product.shop, "product_stock", instance.product_id, "upsert", actor=instance.actor)


@receiver(post_save, sender="customers.CustomerDebt", dispatch_uid="sync_customer_debt_change_feed")
def append_debt_state_to_feed(sender, instance, created, raw=False, **kwargs):
    """US-19/US-21, G6/G7: phones allowed to read debts learn every remaining amount.

    A credit sale creates a debt and a payment, return or regularization lowers
    it, without any debt-specific journal entry; without this, other phones would
    keep a stale balance and a later credit sale could not confirm it.
    """
    if not raw:
        record_change(instance.shop, "customer_debt_state", instance.id, "upsert", actor=instance.actor)
