"""Exact input primitives shared by business API serializers."""
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields) if isinstance(data, dict) else set()
        if unknown:
            raise serializers.ValidationError({name: _("Champ non reconnu.") for name in unknown})
        return super().to_internal_value(data)


@extend_schema_field({"type": "integer", "format": "int64"})
class ExactIntegerField(serializers.IntegerField):
    """Do not silently coerce booleans, decimal numbers or numeric strings."""

    def to_internal_value(self, data):
        if type(data) is not int:
            self.fail("invalid")
        return super().to_internal_value(data)


class StrictBoolean(serializers.BooleanField):
    def to_internal_value(self, data):
        if type(data) is not bool:
            self.fail("invalid", input=data)
        return data
