from drf_spectacular.extensions import OpenApiAuthenticationExtension,OpenApiSerializerExtension


class DeviceJWTScheme(OpenApiAuthenticationExtension):
    target_class = "identity.services.authentication.DeviceJWTAuthentication"
    name = "deviceJWT"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}


class SupplierCreationSourceSchema(OpenApiSerializerExtension):
    """Expose the strict four-field source shape without matching a customer."""
    target_class='backups.services.recovery_supplier_snapshots.FrozenSupplierCreation'

    def map_serializer(self,auto_schema,direction):
        schema=super().map_serializer(auto_schema,direction)
        schema['additionalProperties']=False
        return schema


class SupplierReceiptSchema(OpenApiSerializerExtension):
    """The supplier receipt has exactly its serializer's explicit read fields."""
    target_class='purchasing.api.suppliers.SupplierSerializer'

    def map_serializer(self,auto_schema,direction):
        schema=super().map_serializer(auto_schema,direction)
        schema['additionalProperties']=False
        return schema
