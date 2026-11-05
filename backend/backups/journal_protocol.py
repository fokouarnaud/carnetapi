"""Versioned capture format: preserve copies before any business interpretation."""
import json
import math
from datetime import datetime

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from common.api.fields import ExactIntegerField,StrictSerializer

MAX_JOURNAL_BYTES=2_000_000
MAX_PAYLOAD_BYTES=1_000_000
MAX_OPERATIONS=50
_CREDENTIAL_KEYS={"password","pin","jwt","otp","authorization","access_token","refresh_token"}


def canonical_bytes(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode("utf-8")


def _check_json(value,depth=0):
    if depth>20:raise ValueError()
    if isinstance(value,dict):
        if len(value)>10000:raise ValueError()
        for key,item in value.items():
            if not isinstance(key,str) or key.casefold() in _CREDENTIAL_KEYS:raise ValueError()
            _check_json(item,depth+1)
    elif isinstance(value,list):
        if len(value)>10000:raise ValueError()
        for item in value:_check_json(item,depth+1)
    elif type(value) is float:
        if not math.isfinite(value):raise ValueError()
    elif value is not None and type(value) not in (bool,int,str):raise ValueError()


@extend_schema_field({"type":"object","additionalProperties":True,
    "description":"Opaque source JSON, at most 1000000 UTF-8 canonical bytes, depth 20, no credentials. Supports a full 500-line sale snapshot. Capture does not validate business values."})
class JournalPayloadField(serializers.JSONField):
    def to_internal_value(self,data):
        try:
            if not isinstance(data,dict):raise ValueError()
            _check_json(data)
            if len(canonical_bytes(data))>MAX_PAYLOAD_BYTES:raise ValueError()
        except (ValueError,TypeError,RecursionError,UnicodeError):
            raise serializers.ValidationError(_("Invalid recovery journal payload."),code="recovery_journal_payload_invalid") from None
        return data


class JournalDateTimeField(serializers.DateTimeField):
    def to_internal_value(self,data):
        try:
            parsed=data if isinstance(data,datetime) else parse_datetime(data) if isinstance(data,str) else None
        except (ValueError,TypeError):parsed=None
        if parsed is None or timezone.is_naive(parsed):self.fail("invalid",format="ISO-8601 with UTC offset")
        return super().to_internal_value(parsed)


class JournalOperationInput(StrictSerializer):
    operation_id=serializers.UUIDField()
    origin_installation_id=serializers.UUIDField()
    origin_user_id=serializers.UUIDField()
    operation_type=serializers.CharField(max_length=80,allow_blank=False)
    occurred_at=JournalDateTimeField()
    origin_recorded_at=JournalDateTimeField()
    payload=JournalPayloadField()
    dependencies=serializers.ListField(child=serializers.UUIDField(),max_length=100,default=list)

    def validate_dependencies(self,values):
        if len(set(values))!=len(values):raise serializers.ValidationError(_("Duplicate recovery dependencies."))
        return sorted(values,key=str)


class JournalDepositInput(StrictSerializer):
    operation_id=serializers.UUIDField()
    session_id=serializers.UUIDField()
    shop_id=serializers.UUIDField()
    format_version=ExactIntegerField(min_value=1,max_value=1)
    reason=serializers.CharField(max_length=2000,allow_blank=False)
    operations=JournalOperationInput(many=True,allow_empty=False,max_length=MAX_OPERATIONS)

    def validate_operations(self,values):
        if len({row["operation_id"] for row in values})!=len(values):
            raise serializers.ValidationError(_("Duplicate recovery operation UUIDs."))
        return values
