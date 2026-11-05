"""Decode supplier cash facts and separate original allocations, with exact integers."""
from dataclasses import dataclass
import uuid
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import APIException,ValidationError
from backups.journal_protocol import JournalDateTimeField
from common.api.fields import StrictSerializer,ExactIntegerField
from common.services.operations import json_value
from purchasing.api.supplier_payments import PaymentInput,AllocationInput
from sales.models import SalePayment
from sales.services.pricing import MAX_FCFA


class RecoverySupplierPaymentInvalid(APIException):
    status_code=400
    default_code='recovery_supplier_payment_invalid'
    default_detail=_("Recovery supplier payment or allocation snapshot is inconsistent.")


class FrozenSupplierPaymentPart(StrictSerializer):
    id=serializers.UUIDField()
    method=serializers.ChoiceField(choices=SalePayment.Method.choices)
    other_name=serializers.CharField(max_length=80,allow_blank=True)
    external_reference=serializers.CharField(max_length=150,allow_blank=True)
    amount_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)


class FrozenSupplierPayment(StrictSerializer):
    id=serializers.UUIDField()
    supplier_id=serializers.UUIDField()
    operation_id=serializers.UUIDField()
    installation_id=serializers.UUIDField()
    occurred_at=JournalDateTimeField()
    created_at=JournalDateTimeField()
    amount_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    unallocated_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    parts=FrozenSupplierPaymentPart(many=True,allow_empty=False,max_length=50)
    allocations=serializers.ListField(max_length=0)


class FrozenSupplierAllocationRow(StrictSerializer):
    id=serializers.UUIDField()
    debt_id=serializers.UUIDField()
    amount_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)


class FrozenSupplierPaymentAllocation(StrictSerializer):
    operation_id=serializers.UUIDField()
    supplier_id=serializers.UUIDField()
    payment_id=serializers.UUIDField()
    allocated_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    before_unallocated_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    after_unallocated_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    recorded_at=JournalDateTimeField()
    allocations=FrozenSupplierAllocationRow(many=True,allow_empty=False,max_length=500)


class SupplierPaymentPayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    payment=FrozenSupplierPayment()


class SupplierAllocationPayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    allocation=FrozenSupplierPaymentAllocation()


class SourcePaymentInput(PaymentInput):
    occurred_at=JournalDateTimeField()
    def validate_occurred_at(self,value):return value


@dataclass(frozen=True)
class DecodedSupplierPayment:
    request:dict
    snapshot:dict
    canonical_request:dict


def _payload(capture,payload_type,key,parent_key):
    incoming=payload_type(data=capture.payload);incoming.is_valid(raise_exception=True)
    snapshot=incoming.validated_data[key];raw=incoming.validated_data['original_request']
    if not isinstance(raw,dict):raise RecoverySupplierPaymentInvalid()
    raw=dict(raw)
    if (uuid.UUID(str(raw.pop('installation_id',capture.origin_installation_id)))!=capture.origin_installation_id
            or uuid.UUID(str(raw.pop(parent_key,snapshot[parent_key])))!=snapshot[parent_key]):raise RecoverySupplierPaymentInvalid()
    return raw,snapshot


def decode_supplier_payment(capture):
    try:
        raw,snapshot=_payload(capture,SupplierPaymentPayload,'payment','supplier_id')
        incoming=SourcePaymentInput(data=raw);incoming.is_valid(raise_exception=True);data=incoming.validated_data
        if (data['id']!=snapshot['id'] or data['operation_id']!=capture.operation_id or snapshot['operation_id']!=capture.operation_id
                or snapshot['installation_id']!=capture.origin_installation_id or data['occurred_at']!=capture.occurred_at
                or snapshot['occurred_at']!=capture.occurred_at or snapshot['created_at']<snapshot['occurred_at']
                or snapshot['amount_fcfa']!=sum(row['amount_fcfa'] for row in snapshot['parts'])
                or snapshot['unallocated_fcfa']!=snapshot['amount_fcfa'] or len(data['parts'])!=len(snapshot['parts'])
                or any(asked!=frozen for asked,frozen in zip(data['parts'],snapshot['parts'],strict=True))):raise RecoverySupplierPaymentInvalid()
        return DecodedSupplierPayment(data,snapshot,json_value({**data,'supplier_id':snapshot['supplier_id'],'installation_id':capture.origin_installation_id}))
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):raise RecoverySupplierPaymentInvalid() from None


def decode_supplier_allocation(capture):
    try:
        raw,snapshot=_payload(capture,SupplierAllocationPayload,'allocation','payment_id')
        incoming=AllocationInput(data=raw);incoming.is_valid(raise_exception=True);data=incoming.validated_data
        if (data['operation_id']!=capture.operation_id or snapshot['operation_id']!=capture.operation_id or snapshot['recorded_at']!=capture.occurred_at
                or snapshot['allocated_fcfa']!=sum(row['amount_fcfa'] for row in snapshot['allocations'])
                or snapshot['before_unallocated_fcfa']-snapshot['allocated_fcfa']!=snapshot['after_unallocated_fcfa']
                or len({row['id'] for row in snapshot['allocations']})!=len(snapshot['allocations'])
                or len(data['allocations'])!=len(snapshot['allocations'])
                or any(any(asked[key]!=frozen[key] for key in ('debt_id','amount_fcfa')) for asked,frozen in zip(data['allocations'],snapshot['allocations'],strict=True))):raise RecoverySupplierPaymentInvalid()
        return DecodedSupplierPayment(data,snapshot,json_value({**data,'payment_id':snapshot['payment_id'],'installation_id':capture.origin_installation_id}))
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):raise RecoverySupplierPaymentInvalid() from None


def immutable_payment_receipt(receipt):
    return {key:receipt[key] for key in FrozenSupplierPayment().fields}


def immutable_allocation_receipt(receipt,supplier_id):
    return {'operation_id':receipt['operation_id'],'supplier_id':str(supplier_id),'payment_id':receipt['payment_id'],
        'allocated_fcfa':receipt['allocated_fcfa'],'before_unallocated_fcfa':receipt['after_unallocated_fcfa']+receipt['allocated_fcfa'],
        'after_unallocated_fcfa':receipt['after_unallocated_fcfa'],'recorded_at':receipt['recorded_at'],
        'allocations':[{key:row[key] for key in ('id','debt_id','amount_fcfa')} for row in receipt['allocations']]}
