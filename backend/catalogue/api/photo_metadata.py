"""Public metadata excludes image bytes and retains deletion versions."""
import hashlib
from rest_framework import serializers


class PhotoMetadataSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, allow_null=True)
    product_id = serializers.UUIDField(read_only=True)
    version = serializers.IntegerField(read_only=True)
    present = serializers.BooleanField(read_only=True)
    size_bytes = serializers.IntegerField(read_only=True)
    sha256 = serializers.CharField(read_only=True, allow_null=True)
    mime_type = serializers.CharField(read_only=True, allow_null=True)
    width = serializers.IntegerField(read_only=True)
    height = serializers.IntegerField(read_only=True)


def photo_metadata(photo, product_id):
    data = bytes(photo.data) if photo is not None and photo.data is not None else None
    return {"id": photo.id if photo else None, "product_id": product_id,
            "version": photo.version if photo else 0, "present": data is not None,
            "size_bytes": len(data) if data else 0, "sha256": hashlib.sha256(data).hexdigest() if data else None,
            "mime_type": "image/jpeg" if data else None,
            "width": photo.width if photo else 0, "height": photo.height if photo else 0}
