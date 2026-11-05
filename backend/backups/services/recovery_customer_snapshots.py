"""Decode initial customer and opening-debt facts without using today's balances."""
from dataclasses import dataclass
import uuid
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import APIException,ValidationError

from backups.journal_protocol import JournalDateTimeField
from common.api.fields import ExactIntegerField,StrictSerializer
from common.services.dates import due_at_utc
from common.services.operations import json_value
from customers.api.client_serializers import CreateClientSerializer,PhoneField
from customers.api.debts import OpenDebtSerializer
from sales.services.pricing import MAX_FCFA


class RecoveryCustomerSnapshotInvalid(APIException):
    status_code=400
    default_code="recovery_customer_snapshot_invalid"
    default_detail=_("Recovery customer or opening-debt snapshot is inconsistent.")


class FrozenCustomerCreationSnapshot(StrictSerializer):
    id=serializers.UUIDField()
    name=serializers.CharField(max_length=150,allow_blank=False,trim_whitespace=False)
    phone=PhoneField(required=True)
    version=ExactIntegerField(min_value=1,max_value=1)
    created_at=JournalDateTimeField()
    updated_at=JournalDateTimeField()


class FrozenOpeningDebtSnapshot(StrictSerializer):
    id=serializers.UUIDField()
    customer_id=serializers.UUIDField()
    sale_id=serializers.UUIDField(allow_null=True)
    amount_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    remaining_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    occurred_at=JournalDateTimeField()
    due_at=JournalDateTimeField(allow_null=True)
    due_version=ExactIntegerField(min_value=1,max_value=1)


class FrozenCustomerCreationPayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    customer=FrozenCustomerCreationSnapshot()


class FrozenOpeningDebtPayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    opening_debt=FrozenOpeningDebtSnapshot()


class SourceOpeningDebtRequest(OpenDebtSerializer):
    occurred_at=JournalDateTimeField()


@dataclass(frozen=True)
class DecodedCustomerOperation:
    request:dict
    snapshot:dict
    canonical_request:dict


def _payload(serializer_class,capture,key):
    incoming=serializer_class(data=capture.payload);incoming.is_valid(raise_exception=True)
    raw=incoming.validated_data["original_request"]
    if not isinstance(raw,dict):raise RecoveryCustomerSnapshotInvalid()
    raw=dict(raw)
    if uuid.UUID(str(raw.pop("installation_id",capture.origin_installation_id)))!=capture.origin_installation_id:
        raise RecoveryCustomerSnapshotInvalid()
    return raw,incoming.validated_data[key]


def decode_customer_creation(capture):
    try:
        raw,snapshot=_payload(FrozenCustomerCreationPayload,capture,"customer")
        incoming=CreateClientSerializer(data=raw);incoming.is_valid(raise_exception=True)
        data=incoming.validated_data
        if (data["operation_id"]!=capture.operation_id or data["id"]!=snapshot["id"]
                or data["name"]!=snapshot["name"] or data["phone"]!=snapshot["phone"]
                or snapshot["created_at"]!=capture.occurred_at or snapshot["updated_at"]<snapshot["created_at"]):
            raise RecoveryCustomerSnapshotInvalid()
        return DecodedCustomerOperation(data,snapshot,json_value(data))
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):
        raise RecoveryCustomerSnapshotInvalid() from None


def decode_opening_debt(capture,*,timezone_name):
    try:
        raw,snapshot=_payload(FrozenOpeningDebtPayload,capture,"opening_debt")
        if uuid.UUID(str(raw.pop("customer_id",snapshot["customer_id"])))!=snapshot["customer_id"]:
            raise RecoveryCustomerSnapshotInvalid()
        incoming=SourceOpeningDebtRequest(data=raw);incoming.is_valid(raise_exception=True)
        data=incoming.validated_data
        if (data["operation_id"]!=capture.operation_id or data["id"]!=snapshot["id"]
                or data["amount_fcfa"]!=snapshot["amount_fcfa"] or snapshot["remaining_fcfa"]!=snapshot["amount_fcfa"]
                or snapshot["sale_id"] is not None or data["occurred_at"]!=capture.occurred_at
                or snapshot["occurred_at"]!=capture.occurred_at
                or due_at_utc(data["due_date"],timezone_name)!=snapshot["due_at"]):
            raise RecoveryCustomerSnapshotInvalid()
        canonical=json_value({**data,"customer_id":snapshot["customer_id"],"installation_id":capture.origin_installation_id})
        return DecodedCustomerOperation(data,snapshot,canonical)
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):
        raise RecoveryCustomerSnapshotInvalid() from None


def immutable_customer_snapshot(receipt):
    return {key:receipt[key] for key in FrozenCustomerCreationSnapshot().fields}


def immutable_opening_debt_snapshot(receipt):
    return {key:receipt[key] for key in FrozenOpeningDebtSnapshot().fields}
