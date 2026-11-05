"""Versioned product photos with bounded decoding and atomic SQLite persistence."""
from django.utils.translation import gettext_lazy as _
import hashlib
from io import BytesIO
import re
import uuid
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from drf_spectacular.openapi import AutoSchema
from rest_framework import serializers, viewsets
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response
from rest_framework.renderers import JSONRenderer
from identity.services.authentication import require_live_session
from common.services.uploads import BoundedUploadMixin
from common.api.fields import ExactIntegerField, StrictSerializer
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from common.models import Activity
from catalogue.models import Product, ProductPhoto
from catalogue.api.photo_metadata import PhotoMetadataSerializer, photo_metadata


class PhotoVersionConflict(APIException):
    status_code = 409
    default_detail = _("La photo a changé. Relire sa version avant modification.")
    default_code = "photo_version_conflict"


class MultipartVersionField(ExactIntegerField):
    def to_internal_value(self, value):
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
            value = int(value)
        return super().to_internal_value(value)


class PhotoMutationInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    base_version = ExactIntegerField(min_value=0, max_value=9223372036854775806)
    reason = serializers.CharField(max_length=2000, allow_blank=False)


class PhotoUploadInput(PhotoMutationInput):
    base_version = MultipartVersionField(min_value=0, max_value=9223372036854775806)
    image = serializers.FileField()


def normalize_photo(source):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(source), formats=["JPEG", "PNG", "WEBP", "BMP", "TIFF"]) as probe:
                if getattr(probe, "n_frames", 1) != 1:
                    raise ValidationError("Utiliser une image fixe.")
                probe.verify()
            with Image.open(BytesIO(source), formats=["JPEG", "PNG", "WEBP", "BMP", "TIFF"]) as decoded:
                decoded.draft("RGB", (1280, 1280))
                if decoded.width * decoded.height > 16000000:
                    raise ValidationError("Réduire les dimensions de la photo avant envoi.")
                oriented = ImageOps.exif_transpose(decoded)
                oriented.load()
                if "A" in oriented.getbands() or "transparency" in oriented.info:
                    rgba = oriented.convert("RGBA")
                    normalized = Image.new("RGB", rgba.size, "white")
                    normalized.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    normalized = oriented.convert("RGB")
        normalized.info.clear()
        normalized.thumbnail((640, 640), Image.Resampling.LANCZOS)
        while True:
            for quality in (85, 75, 65, 55):
                output = BytesIO()
                normalized.save(output, format="JPEG", quality=quality, optimize=True, exif=b"", icc_profile=None)
                content = output.getvalue()
                if len(content) <= 20000:
                    return content, normalized.width, normalized.height
            width, height = normalized.size
            if width == height == 1:
                raise ValidationError("La photo ne peut pas respecter la taille maximale.")
            normalized = normalized.resize((max(1, width * 4 // 5), max(1, height * 4 // 5)), Image.Resampling.LANCZOS)
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
        raise ValidationError({"image": _("Image invalide ou trop volumineuse à décoder.")}) from error


class PhotoSchema(AutoSchema):
    def _get_request_body(self, direction="request"):
        if self.method == "DELETE":
            component = self.resolve_serializer(PhotoMutationInput(), direction)
            return {"required": True, "content": {"application/json": {"schema": component.ref}}}
        return super()._get_request_body(direction)


class PhotoDownloadRenderer(JSONRenderer):
    media_type = "image/jpeg"
    format = "jpeg"
    charset = None

    def render(self, data, accepted_media_type=None, renderer_context=None):
        # Successful images are HttpResponse bytes; negotiated error bodies stay JSON.
        renderer_context["response"]["Content-Type"] = "application/json"
        return super().render(data, accepted_media_type, renderer_context)


class ProductPhotoViewSet(BoundedUploadMixin, viewsets.GenericViewSet):
    upload_byte_limit = 10000000
    upload_size_error = {"image": _("Fichier source limité à 10 Mo ; réduire la photo avant envoi.")}
    schema = PhotoSchema()

    def get_renderers(self):
        if getattr(self, "action", None) == "retrieve":
            return [PhotoDownloadRenderer(), JSONRenderer()]
        return super().get_renderers()

    queryset = ProductPhoto.objects.none()
    serializer_class = PhotoMetadataSerializer

    def context(self, manager=False):
        require_live_session(self.request.user, self.request.device_session.id, self.request.device_session.installation)
        shop = current_shop(self.kwargs["shop_id"], self.request.user, self.request.device_session.installation)
        if manager:
            require_manager(shop, self.request.user)
        product = get_object_or_404(Product, shop=shop, id=self.kwargs["product_id"])
        return shop, product

    @extend_schema(responses={(200, "image/jpeg"): {"type": "string", "format": "binary"}})
    @transaction.atomic
    def retrieve(self, request, *args, **kwargs):
        _, product = self.context()
        photo = get_object_or_404(ProductPhoto, product=product, data__isnull=False)
        if "version" in request.query_params:
            version = MultipartVersionField(min_value=1, max_value=9223372036854775807).run_validation(request.query_params["version"])
            if photo.version != version:
                raise PhotoVersionConflict()
        response = HttpResponse(bytes(photo.data), content_type="image/jpeg")
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["ETag"] = '"' + hashlib.sha256(bytes(photo.data)).hexdigest() + '"'
        return response

    @extend_schema(request={"multipart/form-data": PhotoUploadInput}, responses=PhotoMetadataSerializer)
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        shop, product = self.context(manager=True)
        serializer = PhotoUploadInput(data=self.bounded_payload(request))
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        uploaded = data.pop("image")
        if uploaded.size > 10000000:
            raise ValidationError({"image": "Fichier source limité à 10 Mo ; réduire la photo avant envoi."})
        source = uploaded.read(10000001)
        if len(source) > 10000000:
            raise ValidationError({"image": "Fichier source trop volumineux."})
        canonical = json_value({**data, "product_id": product.id, "input_sha256": hashlib.sha256(source).hexdigest()})
        return self.mutate(shop, product, data, canonical, source, "product_photo_updated")

    @extend_schema(request=PhotoMutationInput, responses=PhotoMetadataSerializer)
    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        shop, product = self.context(manager=True)
        serializer = PhotoMutationInput(data=self.bounded_payload(request))
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        canonical = json_value({**data, "product_id": product.id})
        return self.mutate(shop, product, data, canonical, None, "product_photo_removed")

    def mutate(self, shop, product, data, canonical, source, action):
        original = original_receipt(data["operation_id"], shop, self.request.user, action, canonical)
        if original is not None:
            return Response(original)
        photo = ProductPhoto.objects.filter(product=product).first()
        before = json_value(photo_metadata(photo, product.id))
        if data["base_version"] != (photo.version if photo else 0):
            raise PhotoVersionConflict()
        content, width, height = normalize_photo(source) if source is not None else (None, 0, 0)
        if photo is None:
            photo = ProductPhoto.objects.create(id=uuid.uuid5(product.id, "product-photo"), product=product,
                                                data=content, width=width, height=height)
        else:
            photo.data, photo.width, photo.height = content, width, height
            photo.version += 1
            photo.save(update_fields=["data", "width", "height", "version", "updated_at"])
        receipt = json_value(photo_metadata(photo, product.id))
        Activity.objects.create(id=data["operation_id"], shop=shop, actor=self.request.user, action=action,
            target_id=product.id, reason=data["reason"],
            details={"request": canonical, "before": before, "after": receipt, "response": receipt})
        return Response(receipt)
