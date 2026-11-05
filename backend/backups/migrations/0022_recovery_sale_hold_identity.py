"""Bind a pending review to its stable sale UUID without creating a sale."""
import uuid
from django.db import migrations,models


def preserve_sale_identity(apps,schema_editor):
    holds=apps.get_model("backups","RecoverySaleHold")
    sources=holds.objects.values_list("pk","capture__payload__sale__id").iterator(chunk_size=100)
    for hold_id,source_sale_id in sources:
        try:sale_id=uuid.UUID(str(source_sale_id))
        except (ValueError,TypeError,AttributeError):
            raise ValueError("A recovered sale hold has no valid source sale UUID.") from None
        if holds.objects.filter(sale_id=sale_id).exclude(pk=hold_id).exists():
            raise ValueError("Conflicting recovered sale holds require explicit reconciliation.")
        holds.objects.filter(pk=hold_id).update(sale_id=sale_id)


class Migration(migrations.Migration):
    dependencies=[("backups","0021_recovery_sale_holds")]
    operations=[
        migrations.AddField(model_name="recoverysalehold",name="sale_id",field=models.UUIDField(null=True,unique=True)),
        migrations.RunPython(preserve_sale_identity,migrations.RunPython.noop),
        migrations.AlterField(model_name="recoverysalehold",name="sale_id",field=models.UUIDField(unique=True)),
    ]
