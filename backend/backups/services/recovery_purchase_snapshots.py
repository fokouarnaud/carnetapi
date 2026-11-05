"""Decode frozen purchase obligations independently of today's catalogue prices."""
from dataclasses import dataclass
import uuid

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import APIException,ValidationError

from backups.journal_protocol import JournalDateTimeField
from common.api.fields import ExactIntegerField,StrictSerializer
from common.services.operations import json_value
from purchasing.api.purchases import PurchaseInput
from sales.services.pricing import MAX_FCFA


class RecoveryPurchaseInvalid(APIException):
    status_code=400
    default_code='recovery_purchase_invalid'
    default_detail=_("Recovery purchase document snapshot is inconsistent.")


class FrozenPurchaseLine(StrictSerializer):
    id=serializers.UUIDField()
    product_id=serializers.UUIDField()
    pack_id=serializers.UUIDField(allow_null=True)
    product_name=serializers.CharField(max_length=150,trim_whitespace=False)
    unit_label=serializers.CharField(max_length=80,trim_whitespace=False)
    quantity_milli=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    factor_milli=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    base_quantity_milli=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    unit_price_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    total_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    received_quantity_milli=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    awaiting_quantity_milli=ExactIntegerField(min_value=1,max_value=MAX_FCFA)


class FrozenPurchaseDebt(StrictSerializer):
    id=serializers.UUIDField()
    document_id=serializers.UUIDField()
    amount_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    remaining_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    occurred_at=JournalDateTimeField()
    created_at=JournalDateTimeField()


class FrozenPurchaseDocument(StrictSerializer):
    id=serializers.UUIDField()
    supplier_id=serializers.UUIDField()
    kind=serializers.ChoiceField(choices=['order','invoice'])
    order_id=serializers.UUIDField(allow_null=True)
    occurred_at=JournalDateTimeField()
    created_at=JournalDateTimeField()
    external_reference=serializers.CharField(max_length=150,allow_blank=True,trim_whitespace=False)
    total_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    lines=FrozenPurchaseLine(many=True,allow_empty=False,max_length=500)
    debt=FrozenPurchaseDebt(allow_null=True)


class PurchasePayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    purchase=FrozenPurchaseDocument()


class SourcePurchaseInput(PurchaseInput):
    occurred_at=JournalDateTimeField()
    def validate_occurred_at(self,value):return value


@dataclass(frozen=True)
class DecodedPurchase:
    request:dict
    snapshot:dict
    canonical_request:dict


def decode_purchase(capture):
    """Validate creation facts and half-up line amounts without catalogue reads."""
    try:
        incoming=PurchasePayload(data=capture.payload);incoming.is_valid(raise_exception=True)
        snapshot=incoming.validated_data['purchase'];raw=incoming.validated_data['original_request']
        if not isinstance(raw,dict):raise RecoveryPurchaseInvalid()
        raw=dict(raw)
        if (uuid.UUID(str(raw.pop('installation_id',capture.origin_installation_id)))!=capture.origin_installation_id
                or uuid.UUID(str(raw.pop('supplier_id',snapshot['supplier_id'])))!=snapshot['supplier_id']):raise RecoveryPurchaseInvalid()
        request=SourcePurchaseInput(data=raw);request.is_valid(raise_exception=True);data=request.validated_data
        expected_kind={'purchase_order':'order','purchase_invoice':'invoice'}.get(capture.operation_type)
        if (expected_kind is None or snapshot['kind']!=expected_kind or data['id']!=snapshot['id']
                or data['operation_id']!=capture.operation_id or data['occurred_at']!=capture.occurred_at
                or snapshot['occurred_at']!=capture.occurred_at or snapshot['created_at']<capture.occurred_at
                or data['order_id']!=snapshot['order_id'] or data['external_reference']!=snapshot['external_reference']
                or len(data['lines'])!=len(snapshot['lines'])):raise RecoveryPurchaseInvalid()
        total=0
        for asked,frozen in zip(data['lines'],snapshot['lines'],strict=True):
            if any(asked[key]!=frozen[key] for key in ('id','product_id','pack_id','quantity_milli','unit_price_fcfa')):raise RecoveryPurchaseInvalid()
            base,remainder=divmod(frozen['quantity_milli']*frozen['factor_milli'],1000)
            amount=(frozen['quantity_milli']*frozen['unit_price_fcfa']+500)//1000
            if (remainder or base!=frozen['base_quantity_milli'] or base>MAX_FCFA or amount!=frozen['total_fcfa']
                    or frozen['pack_id'] is None and frozen['factor_milli']!=1000
                    or frozen['received_quantity_milli']!=0 or frozen['awaiting_quantity_milli']!=frozen['quantity_milli']):raise RecoveryPurchaseInvalid()
            total+=amount
        if total>MAX_FCFA or snapshot['total_fcfa']!=total:raise RecoveryPurchaseInvalid()
        debt=snapshot['debt']
        if expected_kind=='order':
            if snapshot['order_id'] is not None or debt is not None:raise RecoveryPurchaseInvalid()
        elif (debt is None or debt['document_id']!=snapshot['id'] or debt['amount_fcfa']!=total
                or debt['remaining_fcfa']!=total or debt['occurred_at']!=capture.occurred_at
                or debt['created_at']<snapshot['created_at']):raise RecoveryPurchaseInvalid()
        return DecodedPurchase(data,snapshot,json_value({**data,'supplier_id':snapshot['supplier_id'],'installation_id':capture.origin_installation_id}))
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):raise RecoveryPurchaseInvalid() from None


def immutable_purchase_receipt(receipt,supplier_id):
    """The saved creation receipt includes initial receipt counters and debt balance."""
    return {key:receipt[key] for key in FrozenPurchaseDocument().fields if key!='supplier_id'}|{'supplier_id':str(supplier_id)}
