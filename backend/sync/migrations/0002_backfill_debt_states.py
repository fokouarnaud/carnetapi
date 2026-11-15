"""Debts that existed before the debt-state receiver reach phones too.

Since f6fa076 every CustomerDebt save appends a ``customer_debt_state`` entry
to the shop's change feed. Debts saved before then have no entry, so a phone
that pulls the feed would never learn them, and a later credit sale could not
confirm what the customer owes (US-19). This appends one entry per such debt,
in the shop's own sequence, as the receiver would have done.
"""
from django.db import migrations
from django.db.models import F


def backfill(apps, schema_editor):
    CustomerDebt = apps.get_model("customers", "CustomerDebt")
    ChangeFeedEntry = apps.get_model("sync", "ChangeFeedEntry")
    ShopChangeSequence = apps.get_model("sync", "ShopChangeSequence")
    seen = set(ChangeFeedEntry.objects.filter(object_type="customer_debt_state")
               .values_list("object_id", flat=True))
    for debt in CustomerDebt.objects.order_by("shop_id", "occurred_at", "id"):
        if debt.id in seen:
            continue
        ShopChangeSequence.objects.get_or_create(shop_id=debt.shop_id)
        ShopChangeSequence.objects.filter(shop_id=debt.shop_id).update(last_seq=F("last_seq") + 1)
        seq = ShopChangeSequence.objects.values_list("last_seq", flat=True).get(shop_id=debt.shop_id)
        ChangeFeedEntry.objects.create(shop_id=debt.shop_id, seq=seq, object_type="customer_debt_state",
                                       object_id=debt.id, kind="upsert", actor_id=debt.actor_id)


class Migration(migrations.Migration):

    dependencies = [
        # The earliest state with CustomerDebt (id, shop, actor, occurred_at):
        # a later customers migration here would pin that app's history.
        ("customers", "0001_domain_state"),
        ("sync", "0001_initial"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
