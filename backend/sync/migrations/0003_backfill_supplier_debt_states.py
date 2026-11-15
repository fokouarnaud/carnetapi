"""Supplier debts that existed before their receiver reach managers' phones too.

Every SupplierDebt save now appends a ``supplier_debt_state`` entry to the
shop's change feed. Debts saved before then have none, so a manager's phone
would never learn what was already owed to suppliers (US-27). This appends
one entry per such debt, in the shop's own sequence.
"""
from django.db import migrations
from django.db.models import F


def backfill(apps, schema_editor):
    SupplierDebt = apps.get_model("purchasing", "SupplierDebt")
    ChangeFeedEntry = apps.get_model("sync", "ChangeFeedEntry")
    ShopChangeSequence = apps.get_model("sync", "ShopChangeSequence")
    seen = set(ChangeFeedEntry.objects.filter(object_type="supplier_debt_state")
               .values_list("object_id", flat=True))
    for debt in SupplierDebt.objects.order_by("shop_id", "occurred_at", "id"):
        if debt.id in seen:
            continue
        ShopChangeSequence.objects.get_or_create(shop_id=debt.shop_id)
        ShopChangeSequence.objects.filter(shop_id=debt.shop_id).update(last_seq=F("last_seq") + 1)
        seq = ShopChangeSequence.objects.values_list("last_seq", flat=True).get(shop_id=debt.shop_id)
        ChangeFeedEntry.objects.create(shop_id=debt.shop_id, seq=seq, object_type="supplier_debt_state",
                                       object_id=debt.id, kind="upsert", actor_id=None)


class Migration(migrations.Migration):

    dependencies = [
        # The earliest state holding SupplierDebt (id, shop, occurred_at), so
        # migration-history tests can still roll purchasing back.
        ("purchasing", "0001_domain_state"),
        ("sync", "0002_backfill_debt_states"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
