"""Detail reads carry recovery metadata; original write receipts stay frozen."""
from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field,PolymorphicProxySerializer
from drf_spectacular.serializers import PolymorphicProxySerializerExtension

from .catalogue_serializers import PublicProductSerializer,CostProductSerializer
from backups.api.recovery_metadata import RECOVERY_SCHEMA


class RoleProductResponse(PolymorphicProxySerializer):
    """Authorized representations overlap structurally without a discriminator."""


class RoleProductResponseExtension(PolymorphicProxySerializerExtension):
    target_class="catalogue.api.recovery_serializers.RoleProductResponse"
    priority=1

    def map_serializer(self,auto_schema,direction):
        result=super().map_serializer(auto_schema,direction)
        result["anyOf"]=result.pop("oneOf")
        return result


class PublicRecoveryProductSerializer(PublicProductSerializer):
    restoration=serializers.SerializerMethodField()

    @extend_schema_field(RECOVERY_SCHEMA)
    def get_restoration(self,product):
        return self.context.get("restoration")

    class Meta(PublicProductSerializer.Meta):
        fields=PublicProductSerializer.Meta.fields+["restoration"]
        read_only_fields=fields


class CostRecoveryProductSerializer(CostProductSerializer):
    restoration=serializers.SerializerMethodField()
    get_restoration=PublicRecoveryProductSerializer.get_restoration

    class Meta(CostProductSerializer.Meta):
        fields=CostProductSerializer.Meta.fields+["restoration"]
        read_only_fields=fields
