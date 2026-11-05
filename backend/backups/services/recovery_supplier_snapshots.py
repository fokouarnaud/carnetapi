"""Strict initial supplier facts, never a later payable or available credit."""
from dataclasses import dataclass
import uuid
from django.core.validators import RegexValidator
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import APIException,ValidationError
from backups.journal_protocol import JournalDateTimeField
from common.api.fields import ExactIntegerField,StrictSerializer
from common.services.operations import json_value
from purchasing.api.suppliers import CreateSupplierSerializer
from purchasing.api.purchases import OpeningDebtInput
from purchasing.api.supplier_credits import OpeningBalanceInput
from sales.services.pricing import MAX_FCFA


class RecoverySupplierSnapshotInvalid(APIException):
    status_code=400
    default_code='recovery_supplier_snapshot_invalid'
    default_detail=_("Recovery supplier or opening-balance snapshot is inconsistent.")


class FrozenSupplierCreation(StrictSerializer):
    id=serializers.UUIDField()
    name=serializers.CharField(max_length=150,allow_blank=False,trim_whitespace=False)
    phone=serializers.CharField(max_length=16,allow_null=True,validators=[RegexValidator(r'^\+[1-9][0-9]{7,14}$')])
    version=ExactIntegerField(min_value=1,max_value=1)


class FrozenSupplierOpeningDebt(StrictSerializer):
    id=serializers.UUIDField()
    supplier_id=serializers.UUIDField()
    document_id=serializers.UUIDField(allow_null=True)
    amount_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    remaining_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    occurred_at=JournalDateTimeField()
    created_at=JournalDateTimeField()


class FrozenSupplierOpeningBalance(StrictSerializer):
    id=serializers.UUIDField()
    supplier_id=serializers.UUIDField()
    kind=serializers.ChoiceField(choices=['opening_advance','opening_credit'])
    return_id=serializers.UUIDField(allow_null=True)
    reference_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA,allow_null=True)
    amount_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    applied_to_origin_fcfa=ExactIntegerField(min_value=0,max_value=0)
    remaining_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    occurred_at=JournalDateTimeField()
    created_at=JournalDateTimeField()


class SupplierCreationPayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    supplier=FrozenSupplierCreation()


class SupplierDebtPayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    opening_debt=FrozenSupplierOpeningDebt()


class SupplierBalancePayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    opening_balance=FrozenSupplierOpeningBalance()


class SourceSupplierDebtInput(OpeningDebtInput):
    occurred_at=JournalDateTimeField()
    def validate_occurred_at(self,value):return value


class SourceSupplierBalanceInput(OpeningBalanceInput):
    occurred_at=JournalDateTimeField()
    def validate_occurred_at(self,value):return value


@dataclass(frozen=True)
class DecodedSupplierOperation:
    request:dict
    snapshot:dict
    canonical_request:dict


def decode_supplier_operation(capture):
    try:
        is_creation=capture.operation_type=='supplier_creation';is_debt=capture.operation_type=='supplier_opening_debt'
        payload_type=SupplierCreationPayload if is_creation else SupplierDebtPayload if is_debt else SupplierBalancePayload
        key='supplier' if is_creation else 'opening_debt' if is_debt else 'opening_balance'
        incoming=payload_type(data=capture.payload);incoming.is_valid(raise_exception=True)
        snapshot=incoming.validated_data[key];raw=incoming.validated_data['original_request']
        if not isinstance(raw,dict):raise RecoverySupplierSnapshotInvalid()
        raw=dict(raw)
        if uuid.UUID(str(raw.pop('installation_id',capture.origin_installation_id)))!=capture.origin_installation_id:raise RecoverySupplierSnapshotInvalid()
        if not is_creation and uuid.UUID(str(raw.pop('supplier_id',snapshot['supplier_id'])))!=snapshot['supplier_id']:raise RecoverySupplierSnapshotInvalid()
        request_type=CreateSupplierSerializer if is_creation else SourceSupplierDebtInput if is_debt else SourceSupplierBalanceInput
        request=request_type(data=raw);request.is_valid(raise_exception=True);data=request.validated_data
        if data['operation_id']!=capture.operation_id or data['id']!=snapshot['id']:raise RecoverySupplierSnapshotInvalid()
        if is_creation:
            if any(data[key]!=snapshot[key] for key in ('name','phone')):raise RecoverySupplierSnapshotInvalid()
        elif (data['amount_fcfa']!=snapshot['amount_fcfa'] or snapshot['remaining_fcfa']!=snapshot['amount_fcfa']
                or data['occurred_at']!=capture.occurred_at or snapshot['occurred_at']!=capture.occurred_at
                or snapshot['created_at']<snapshot['occurred_at']):raise RecoverySupplierSnapshotInvalid()
        elif is_debt:
            if snapshot['document_id'] is not None:raise RecoverySupplierSnapshotInvalid()
        elif (snapshot['kind']!='opening_'+data['balance_kind'] or snapshot['return_id'] is not None
                or snapshot['reference_fcfa'] is not None or snapshot['applied_to_origin_fcfa']!=0):raise RecoverySupplierSnapshotInvalid()
        canonical={**data,'installation_id':capture.origin_installation_id}
        if not is_creation:canonical['supplier_id']=snapshot['supplier_id']
        return DecodedSupplierOperation(data,snapshot,json_value(canonical))
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):raise RecoverySupplierSnapshotInvalid() from None
