from decimal import Decimal

from django.db import migrations, models


def copy_costs(apps, schema_editor):
    Movement = apps.get_model("core", "StockMovement")
    for movement in Movement.objects.using(schema_editor.connection.alias).exclude(unit_cost_fcfa=None).iterator():
        micros = movement.unit_cost_fcfa * Decimal(1000000)
        if micros != micros.to_integral_value() or not 0 <= micros <= 9223372036854775807:
            raise ValueError("Coût initial hors plage ; migration interrompue sans troncature.")
        Movement.objects.using(schema_editor.connection.alias).filter(pk=movement.pk).update(unit_cost_micros=int(micros))


def restore_costs(apps, schema_editor):
    Movement = apps.get_model("core", "StockMovement")
    for movement in Movement.objects.using(schema_editor.connection.alias).exclude(unit_cost_micros=None).iterator():
        Movement.objects.using(schema_editor.connection.alias).filter(pk=movement.pk).update(
            unit_cost_fcfa=Decimal(movement.unit_cost_micros) / Decimal(1000000),
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0004_product_stockmovement_and_more")]
    operations = [
        migrations.AddField("stockmovement", "unit_cost_micros", models.PositiveBigIntegerField(blank=True, null=True)),
        migrations.RunPython(copy_costs, restore_costs),
        migrations.RemoveField("stockmovement", "unit_cost_fcfa"),
    ]
