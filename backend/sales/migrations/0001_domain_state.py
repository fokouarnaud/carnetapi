"""Adopt existing tables; the atomic state relocation is in core.0081."""
from django.db import migrations


class Migration(migrations.Migration):
    initial = True
    dependencies = [("core", "0081_domain_apps")]
    operations = []
