"""Durable, bounded CSV previews; only a separate confirmation may create products."""
import csv
import hashlib
from io import StringIO
import re
import uuid
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.parsers import MultiPartParser
from common.services.parsers import BoundedJSONParser
from rest_framework.response import Response
from rest_framework.renderers import JSONRenderer
from identity.services.authentication import require_live_session
from common.api.fields import StrictSerializer
from catalogue.api.catalogue_serializers import CreateProductSerializer
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from common.services.errors import OperationConflict
from common.services.uploads import BoundedUploadMixin
from common.services.csv_sources import read_source, preview_result
from common.models import Activity
from catalogue.models import Barcode, CatalogueImport, Category
from catalogue.services.catalogue_services import create_product, product_request_value


class ImportUploadInput(StrictSerializer):
    id = serializers.UUIDField()
    file = serializers.FileField()


class ImportRowSerializer(serializers.Serializer):
    line = serializers.IntegerField()
    data = serializers.JSONField()
    errors = serializers.JSONField()


class ImportPreviewSerializer(serializers.Serializer):
    kind = serializers.CharField(read_only=True)
    id = serializers.UUIDField()
    source_sha256 = serializers.CharField()
    applied = serializers.BooleanField()
    row_count = serializers.IntegerField()
    valid_count = serializers.IntegerField()
    has_errors = serializers.BooleanField()
    file_errors = serializers.JSONField()
    rows = ImportRowSerializer(many=True)


def import_context(request, shop_id):
    session = request.device_session
    require_live_session(request.user, session.id, session.installation)
    shop = current_shop(shop_id, request.user, session.installation)
    require_manager(shop, request.user)
    return shop


def preview_import(record):
    if record.kind != "products":
        from catalogue.services.party_imports import preview_parties
        return preview_parties(record)
    required = {"name", "unit_kind", "unit_label", "sale_price_fcfa"}
    optional = {"purchase_price_fcfa", "stock_threshold_milli", "category_id", "barcode", "opening_quantity_milli",
                "opening_unit_cost_fcfa", "opening_occurred_at", "opening_reason"}
    rows, file_errors = read_source(record, required, optional)
    candidates, used_codes = [], set()
    for row in rows:
        if row["errors"]:
            continue
        line = row["line"]
        data = dict(row["data"])
        raw = dict(data)
        errors = {}
        for field in ("sale_price_fcfa", "purchase_price_fcfa", "stock_threshold_milli", "opening_quantity_milli"):
            if field not in data:
                continue
            value = data[field]
            if value == "" and field != "sale_price_fcfa":
                data[field] = None
            elif len(value) <= 30 and re.fullmatch(r"-?[0-9]+", value):
                data[field] = int(value)
            else:
                errors[field] = gettext_lazy("Un entier est requis.")
        for field in ("category_id", "barcode"):
            if data.get(field) == "":
                data[field] = None
        opening = {"quantity_milli": data.pop("opening_quantity_milli", None),
                   "unit_cost_fcfa": data.pop("opening_unit_cost_fcfa", "") or None,
                   "occurred_at": data.pop("opening_occurred_at", ""),
                   "reason": data.pop("opening_reason", "")}
        if opening["quantity_milli"] is not None:
            data["opening"] = opening
        elif any(opening[name] not in (None, "") for name in ("unit_cost_fcfa", "occurred_at", "reason")):
            errors["opening_quantity_milli"] = gettext_lazy("Renseigner la quantité du stock initial.")
        data.update(id=uuid.uuid5(record.id, f"product:{line}"), operation_id=uuid.uuid5(record.id, f"operation:{line}"))
        serializer = CreateProductSerializer(data=data)
        if not errors:
            if serializer.is_valid():
                validated = serializer.validated_data
                category = validated["category_id"]
                if record.applied_operation_id is None and category is not None and not Category.objects.filter(shop_id=record.shop_id, id=category).exists():
                    errors["category_id"] = gettext_lazy("Catégorie absente de cette boutique.")
                code = validated["barcode"]
                if code is not None:
                    collision = Barcode.objects.filter(shop_id=record.shop_id, code=code)
                    if record.applied_operation_id is not None:
                        collision = collision.exclude(product_id=validated["id"], pack__isnull=True)
                    if code in used_codes or (record.applied_operation_id is None and collision.exists()):
                        errors["barcode"] = gettext_lazy("Code déjà associé dans cette boutique ou ce fichier.")
                    used_codes.add(code)
                if not errors:
                    candidates.append(validated)
                    raw = product_request_value(validated)
            else:
                errors = serializer.errors
        row["data"], row["errors"] = raw, errors
    return preview_result(record, rows, candidates, file_errors)


class ImportConfirmationInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    source_sha256 = serializers.RegexField(regex=r"^[0-9a-f]{64}$", min_length=64, max_length=64)
    reason = serializers.CharField(max_length=2000, allow_blank=False)


class ImportAppliedSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    source_sha256 = serializers.CharField()
    applied = serializers.BooleanField()
    created_count = serializers.IntegerField()
    product_ids = serializers.ListField(child=serializers.UUIDField(), required=False)
    customer_ids = serializers.ListField(child=serializers.UUIDField(), required=False)
    supplier_ids = serializers.ListField(child=serializers.UUIDField(), required=False)
    opening_debt_ids = serializers.ListField(child=serializers.UUIDField(), required=False)
    opening_balance_ids = serializers.ListField(child=serializers.UUIDField(), required=False)


class ImportInvalid(APIException):
    status_code = 409
    default_detail = gettext_lazy("Corriger les erreurs du fichier avant confirmation.")
    default_code = "catalogue_import_invalid"

    def __init__(self, preview):
        super().__init__()
        self.detail = {"code": self.default_code, "detail": self.detail, "preview": preview}


class ImportAlreadyApplied(APIException):
    status_code = 409
    default_detail = gettext_lazy("Ce fichier a déjà été appliqué.")
    default_code = "catalogue_import_already_applied"


class ImportSourceConflict(APIException):
    status_code = 409
    default_detail = gettext_lazy("Confirmer l'empreinte du fichier prévisualisé.")
    default_code = "catalogue_import_source_conflict"


class CsvTemplateRenderer(JSONRenderer):
    media_type = "text/csv"
    format = "csv"
    charset = None

    def render(self, data, accepted_media_type=None, renderer_context=None):
        renderer_context["response"]["Content-Type"] = "application/json"
        return super().render(data, accepted_media_type, renderer_context)


class CatalogueImportViewSet(BoundedUploadMixin, viewsets.GenericViewSet):
    kind = "products"
    upload_byte_limit = 1000000
    upload_size_error = {"file": gettext_lazy("Fichier limité à 1000000 octets.")}
    queryset = CatalogueImport.objects.none()
    serializer_class = ImportPreviewSerializer
    parser_classes = [MultiPartParser, BoundedJSONParser]

    def get_renderers(self):
        if getattr(self, "action", None) == "template":
            return [CsvTemplateRenderer(), JSONRenderer()]
        return super().get_renderers()

    @extend_schema(request={"multipart/form-data": ImportUploadInput}, responses={200: ImportPreviewSerializer, 201: ImportPreviewSerializer})
    @transaction.atomic
    def create(self, request, *args, **kwargs):
        shop = import_context(request, self.kwargs["shop_id"])
        serializer = ImportUploadInput(data=self.bounded_payload(request))
        serializer.is_valid(raise_exception=True)
        upload = serializer.validated_data["file"]
        source = upload.read(1000001)
        if len(source) > 1000000:
            raise ValidationError({"file": gettext_lazy("Fichier limité à 1000000 octets.")})
        identifier = serializer.validated_data["id"]
        existing = CatalogueImport.objects.filter(id=identifier).first()
        if existing is not None:
            if existing.kind != self.kind or existing.shop_id != shop.id or existing.actor_id != request.user.id or bytes(existing.source) != source:
                raise OperationConflict()
            return Response(preview_import(existing)[0])
        record = CatalogueImport.objects.create(id=identifier, kind=self.kind, shop=shop, actor=request.user,
            installation=request.device_session.installation, source=source, source_sha256=hashlib.sha256(source).hexdigest())
        return Response(preview_import(record)[0], status=201)

    @extend_schema(responses=ImportPreviewSerializer)
    @transaction.atomic
    def retrieve(self, request, *args, **kwargs):
        shop = import_context(request, self.kwargs["shop_id"])
        record = get_object_or_404(CatalogueImport, kind=self.kind, shop=shop, id=self.kwargs["pk"])
        return Response(preview_import(record)[0])


    @extend_schema(request=ImportConfirmationInput, responses=ImportAppliedSerializer)
    @transaction.atomic
    def confirm(self, request, *args, **kwargs):
        shop = import_context(request, self.kwargs["shop_id"])
        record = get_object_or_404(CatalogueImport, kind=self.kind, shop=shop, id=self.kwargs["pk"])
        serializer = ImportConfirmationInput(data=self.bounded_payload(request))
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        canonical = json_value({**data, "import_id": record.id})
        action = "catalogue_import_applied" if self.kind == "products" else f"{self.kind}_import_applied"
        original = original_receipt(data["operation_id"], shop, request.user, action, canonical)
        if original is not None:
            return Response(original)
        if record.applied_operation_id is not None:
            raise ImportAlreadyApplied()
        if data["source_sha256"] != record.source_sha256:
            raise ImportSourceConflict()
        preview, candidates = preview_import(record)
        if preview["has_errors"] or not preview["row_count"]:
            raise ImportInvalid(preview)
        identifiers, debt_ids, balance_ids = [], [], []
        for candidate in candidates:
            if self.kind == "products":
                receipt, _ = create_product(shop.id, request.user, request.device_session.installation,
                                             candidate, request.device_session.id)
            else:
                from catalogue.services.party_imports import apply_party
                receipt, debt_id, row_balance_ids = apply_party(self.kind, shop, request, candidate)
                balance_ids.extend(row_balance_ids)
                if debt_id is not None:
                    debt_ids.append(debt_id)
            identifiers.append(receipt["id"])
        key = {"products": "product_ids", "customers": "customer_ids", "suppliers": "supplier_ids"}[self.kind]
        receipt = {"id": str(record.id), "operation_id": str(data["operation_id"]), "source_sha256": record.source_sha256,
                   "applied": True, "created_count": len(identifiers), key: identifiers}
        if self.kind != "products":
            receipt["opening_debt_ids"] = debt_ids
        if self.kind == "suppliers":
            receipt["opening_balance_ids"] = balance_ids
        if Activity.objects.filter(id=data["operation_id"]).exists():
            raise OperationConflict()
        event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=request.user,
            action=action, target_id=record.id, reason=data["reason"],
            details={"request": canonical, "response": receipt, "installation_id": str(request.device_session.installation_id)})
        record.applied_operation = event
        record.response = receipt
        record.save(update_fields=["applied_operation", "response", "updated_at"])
        return Response(receipt)


    @extend_schema(responses={(200, "text/csv"): {"type": "string", "format": "binary"}})
    @transaction.atomic
    def template(self, request, *args, **kwargs):
        import_context(request, self.kwargs["shop_id"])
        output = StringIO(newline="")
        writer = csv.writer(output)
        if self.kind == "products":
            writer.writerow(["name", "unit_kind", "unit_label", "sale_price_fcfa", "purchase_price_fcfa",
                             "stock_threshold_milli", "category_id", "barcode", "opening_quantity_milli",
                             "opening_unit_cost_fcfa", "opening_occurred_at", "opening_reason"])
            writer.writerow([str(gettext_lazy("Exemple de produit")), "piece", str(gettext_lazy("pièce")), 250,
                             "", "", "", "", "", "", "", ""])
        else:
            headers = ["name", "phone", "opening_debt_fcfa", "opening_occurred_at", "opening_reason"]
            if self.kind == "customers":
                headers.append("due_date")
            else:
                headers.extend(["opening_advance_fcfa", "opening_credit_fcfa"])
            writer.writerow(headers)
            label = gettext_lazy("Exemple de client") if self.kind == "customers" else gettext_lazy("Exemple de fournisseur")
            writer.writerow([str(label)] + [""] * (len(headers) - 1))
        response = HttpResponse(output.getvalue().encode("utf-8-sig"), content_type="text/csv; charset=utf-8")
        filename = "catalogue-template.csv" if self.kind == "products" else f"{self.kind}-template.csv"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
