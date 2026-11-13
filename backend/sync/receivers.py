"""Every journaled shop write appends one feed entry in the same transaction (E7)."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from common.models import Activity
from sync.services import record_change


@receiver(post_save, sender=Activity, dispatch_uid="sync_activity_change_feed")
def append_activity_to_feed(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        record_change(instance.shop, instance.action, instance.target_id, "upsert", actor=instance.actor)
