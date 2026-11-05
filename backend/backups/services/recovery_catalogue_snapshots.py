"""Validate versioned catalogue deltas without accepting stock or historical costs."""
from dataclasses import dataclass
import uuid

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import APIException, ValidationError

from backups.journal_protocol import JournalDateTimeField
from catalogue.api.catalogue_serializers import UpdateProductSerializer, UpdatePackSerializer
from catalogue.models import Product
from common.api.fields import ExactIntegerField, StrictBoolean, StrictSerializer
from common.services.operations import json_value
from sales.services.pricing import MAX_FCFA


class RecoveryCatalogueRevisionInvalid(APIException):
    status_code = 400
    default_code = "recovery_catalogue_revision_invalid"
    default_detail = _("Recovery catalogue revision snapshot is inconsistent.")


class FrozenCatalogueState(StrictSerializer):
    id = serializers.UUIDField()
    name = serializers.CharField(max_length=150, allow_blank=False, trim_whitespace=False)
    sale_price_fcfa = ExactIntegerField(min_value=0, max_value=MAX_FCFA)
    is_active = StrictBoolean()
    version = ExactIntegerField(min_value=1, max_value=MAX_FCFA)
    created_at = JournalDateTimeField()
    updated_at = JournalDateTimeField()


class FrozenProductState(FrozenCatalogueState):
    shop_id = serializers.UUIDField()
    category_id = serializers.UUIDField(allow_null=True)
    unit_kind = serializers.ChoiceField(choices=Product.UnitKind.choices)
    unit_label = serializers.CharField(max_length=40, allow_blank=False, trim_whitespace=False)
    purchase_price_fcfa = ExactIntegerField(min_value=0, max_value=MAX_FCFA, allow_null=True)
    stock_threshold_milli = ExactIntegerField(min_value=0, max_value=MAX_FCFA, allow_null=True)


class FrozenPackState(FrozenCatalogueState):
    name = serializers.CharField(max_length=80, allow_blank=False, trim_whitespace=False)
    product_id = serializers.UUIDField()
    product_version = ExactIntegerField(min_value=1, max_value=MAX_FCFA)
    product_updated_at = JournalDateTimeField()
    base_quantity_milli = ExactIntegerField(min_value=1, max_value=MAX_FCFA)


class ProductRevisionSnapshot(StrictSerializer):
    before = FrozenProductState()
    after = FrozenProductState()


class PackRevisionSnapshot(StrictSerializer):
    before = FrozenPackState()
    after = FrozenPackState()


class ProductRevisionPayload(StrictSerializer):
    schema_version = ExactIntegerField(min_value=1, max_value=1)
    original_request = serializers.JSONField()
    catalogue_revision = ProductRevisionSnapshot()


class PackRevisionPayload(ProductRevisionPayload):
    catalogue_revision = PackRevisionSnapshot()


@dataclass(frozen=True)
class DecodedCatalogueRevision:
    request: dict
    snapshot: dict
    canonical_request: dict


def immutable_catalogue_state(receipt, *, pack):
    fields = FrozenPackState().fields if pack else FrozenProductState().fields
    return {key: receipt[key] for key in fields}


def decode_catalogue_revision(capture):
    try:
        pack = capture.operation_type == "pack_update"
        if capture.operation_type not in ("product_update", "pack_update"):
            raise RecoveryCatalogueRevisionInvalid()
        incoming = (PackRevisionPayload if pack else ProductRevisionPayload)(data=capture.payload)
        incoming.is_valid(raise_exception=True)
        snapshot = incoming.validated_data["catalogue_revision"]
        before, after = snapshot["before"], snapshot["after"]
        raw = incoming.validated_data["original_request"]
        if not isinstance(raw, dict):raise RecoveryCatalogueRevisionInvalid()
        raw = dict(raw)
        if uuid.UUID(str(raw.pop("installation_id", capture.origin_installation_id))) != capture.origin_installation_id:
            raise RecoveryCatalogueRevisionInvalid()
        product_id = before["product_id"] if pack else before["id"]
        if uuid.UUID(str(raw.pop("product_id", product_id))) != product_id:
            raise RecoveryCatalogueRevisionInvalid()
        if pack and uuid.UUID(str(raw.pop("pack_id", before["id"]))) != before["id"]:
            raise RecoveryCatalogueRevisionInvalid()
        request = (UpdatePackSerializer if pack else UpdateProductSerializer)(data=raw)
        request.is_valid(raise_exception=True)
        data = request.validated_data
        if (data["operation_id"] != capture.operation_id or data["base_version"] != before["version"]
                or after["version"] != before["version"] + 1
                or after["updated_at"] != capture.occurred_at
                or before["created_at"] > before["updated_at"] or before["updated_at"] > capture.occurred_at):
            raise RecoveryCatalogueRevisionInvalid()
        changes = set(data) - {"operation_id", "base_version", "reason"}
        for key in before:
            expected = (data[key] if key in changes else before[key])
            if key == "version":expected = before[key] + 1
            elif key == "updated_at":expected = capture.occurred_at
            elif pack and key == "product_version":expected = before[key] + 1
            elif pack and key == "product_updated_at":
                if (before[key] > capture.occurred_at or after[key] < before[key] or after[key] < capture.occurred_at
                        or after[key] > capture.origin_recorded_at):raise RecoveryCatalogueRevisionInvalid()
                continue
            if after[key] != expected:raise RecoveryCatalogueRevisionInvalid()
        if not pack:
            if before["shop_id"] != capture.shop_id:raise RecoveryCatalogueRevisionInvalid()
            for state in (before, after):
                threshold = state["stock_threshold_milli"]
                if state["unit_kind"] == "piece" and threshold is not None and threshold % 1000:
                    raise RecoveryCatalogueRevisionInvalid()
        canonical = json_value({**data, "product_id": product_id, **({"pack_id": before["id"]} if pack else {})})
        return DecodedCatalogueRevision(data, snapshot, canonical)
    except (ValidationError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise RecoveryCatalogueRevisionInvalid() from None
