import uuid

from django.db import models
from django.db.models.lookups import Exact


def _scaled_integer_product(left, right, rounding=0):
    """Keep SQLite intermediates within int64 for valid scaled quantities."""
    left, right = models.F(left), models.F(right)
    return ((left / 1000) * right + (left % 1000) * (right / 1000)
            + ((left % 1000) * (right % 1000) + rounding) / 1000)


def _integer_columns(*names):
    """SQLite affinity alone does not prohibit fractional stored money."""
    condition = models.Q()
    for name in names:
        condition &= Exact(models.Func(models.F(name), function="typeof", output_field=models.CharField()), models.Value("integer"))
    return condition


class UUIDRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
