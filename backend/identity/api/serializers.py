from rest_framework import serializers

from identity.models import Membership, Shop


class ShopSerializer(serializers.ModelSerializer):
    class Meta:
        model = Shop
        fields = ["id", "name", "timezone", "created_at", "updated_at"]
        read_only_fields = fields


class VerifyAccessRequestSerializer(serializers.Serializer):
    pass


class AccessLeaseSerializer(serializers.Serializer):
    shop_id = serializers.UUIDField()
    installation_id = serializers.UUIDField()
    verified_at = serializers.DateTimeField()
    offline_expires_at = serializers.DateTimeField()
    role = serializers.ChoiceField(choices=Membership.Role.choices)
    permissions = serializers.DictField(child=serializers.BooleanField())
    seller_discount_cap_basis_points = serializers.IntegerField(min_value=0, max_value=10000)
    discount_policy_version = serializers.IntegerField(min_value=1)


class RevokeDeviceSerializer(serializers.Serializer):
    installation_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000, allow_blank=False, trim_whitespace=True)
    operation_id = serializers.UUIDField()


class RevokeDeviceResultSerializer(serializers.Serializer):
    installation_id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    revoked_at = serializers.DateTimeField()
