from rest_framework import serializers

from common.api.fields import ExactIntegerField, StrictSerializer
from common.models import Activity
from customers.models import Customer


class PhoneField(serializers.RegexField):
    def __init__(self, **kwargs):
        super().__init__(r"^\+[1-9][0-9]{7,14}$", max_length=16, allow_null=True, allow_blank=True, **kwargs)

    def run_validation(self, data=serializers.empty):
        value = super().run_validation(data)
        return value or None


class ClientSerializer(serializers.ModelSerializer):
    has_possible_duplicate = serializers.BooleanField(read_only=True)

    class Meta:
        model = Customer
        fields = ["id", "shop_id", "name", "phone", "version", "has_possible_duplicate", "created_at", "updated_at"]
        read_only_fields = fields


class CreateClientSerializer(StrictSerializer):
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    name = serializers.CharField(max_length=150, trim_whitespace=True, allow_blank=False)
    phone = PhoneField(default=None)


class UpdateClientSerializer(StrictSerializer):
    operation_id = serializers.UUIDField()
    base_version = ExactIntegerField(min_value=1, max_value=9223372036854775806)
    reason = serializers.CharField(max_length=2000, trim_whitespace=True, allow_blank=False)
    name = serializers.CharField(max_length=150, trim_whitespace=True, allow_blank=False, required=False)
    phone = PhoneField(required=False)

    def validate(self, data):
        if not {"name", "phone"}.intersection(data):
            raise serializers.ValidationError("Indiquer au moins un champ à corriger.")
        return data


class ReportCorrectionSerializer(StrictSerializer):
    operation_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000, trim_whitespace=True, allow_blank=False)


class CorrectionReceiptSerializer(serializers.Serializer):
    operation_id = serializers.UUIDField()
    client_id = serializers.UUIDField()
    reported_at = serializers.DateTimeField()


class ClientHistorySerializer(serializers.ModelSerializer):
    actor_id=serializers.UUIDField(source="public_actor_id",read_only=True)
    class Meta:
        model = Activity
        fields = ["id", "actor_id", "target_id", "action", "reason", "details", "created_at"]
        read_only_fields = fields
