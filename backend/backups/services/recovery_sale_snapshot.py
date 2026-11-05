"""Verify frozen sale arithmetic independently from today's catalogue and rights."""
from dataclasses import dataclass
import uuid

from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import APIException,ValidationError

from backups.journal_protocol import JournalDateTimeField
from common.api.fields import ExactIntegerField,StrictSerializer
from common.services.dates import due_at_utc
from common.services.operations import json_value
from catalogue.models import Product
from sales.api.sale_serializers import CreateSaleSerializer,DeclaredPaymentSerializer
from sales.services.pricing import LineAmount,MAX_FCFA,PricingFailure,PricedBasket,line_reference_amount,price_basket


class RecoverySaleSnapshotInvalid(APIException):
    status_code=400
    default_code="recovery_sale_snapshot_invalid"
    default_detail=_("Recovery sale snapshot is inconsistent.")


def money(*,positive=False):
    return ExactIntegerField(min_value=1 if positive else 0,max_value=MAX_FCFA)


class FrozenSaleLine(StrictSerializer):
    id=serializers.UUIDField()
    product_id=serializers.UUIDField()
    product_version=money(positive=True)
    pack_id=serializers.UUIDField(allow_null=True)
    product_name=serializers.CharField(max_length=150,trim_whitespace=False)
    unit_kind=serializers.ChoiceField(choices=Product.UnitKind.choices)
    unit_label=serializers.CharField(max_length=80,trim_whitespace=False)
    quantity_milli=money(positive=True)
    factor_milli=money(positive=True)
    base_quantity_milli=money(positive=True)
    unit_price_fcfa=money()
    reference_fcfa=money()
    own_discount_fcfa=money()
    global_discount_fcfa=money()
    net_fcfa=money()


class FrozenSalePayment(DeclaredPaymentSerializer):
    received_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA,allow_null=True,default=None)
    returned_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA,allow_null=True,default=None)

    def validate(self,data):
        data=super().validate(data)
        received,returned=data["received_fcfa"],data["returned_fcfa"]
        if (received is None)!=(returned is None) or (received is not None and received-returned!=data["amount_fcfa"]):
            raise serializers.ValidationError("Invalid frozen change.")
        return data


class FrozenSaleSnapshot(StrictSerializer):
    id=serializers.UUIDField()
    mode=serializers.ChoiceField(choices=["cash","credit"])
    customer_id=serializers.UUIDField(allow_null=True)
    due_at=JournalDateTimeField(allow_null=True)
    reference_total_fcfa=money()
    discount_total_fcfa=money()
    net_total_fcfa=money()
    lines=FrozenSaleLine(many=True,allow_empty=False,max_length=500)
    payments=FrozenSalePayment(many=True,max_length=50)


class FrozenSalePayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    sale=FrozenSaleSnapshot()


@dataclass(frozen=True)
class DecodedSale:
    request:dict
    snapshot:dict
    basket:PricedBasket
    canonical_request:dict


def decode_sale_snapshot(capture,*,timezone_name):
    """Check source agreement and exact arithmetic; never reprice from Product."""
    try:
        serializer=FrozenSalePayload(data=capture.payload)
        serializer.is_valid(raise_exception=True)
        snapshot=serializer.validated_data["sale"]
        raw=serializer.validated_data["original_request"]
        if not isinstance(raw,dict):raise RecoverySaleSnapshotInvalid()
        raw=dict(raw)
        original_installation=raw.pop("installation_id",str(capture.origin_installation_id))
        if uuid.UUID(str(original_installation))!=capture.origin_installation_id:raise RecoverySaleSnapshotInvalid()
        request=CreateSaleSerializer(data=raw)
        request.is_valid(raise_exception=True)
        data=request.validated_data
        if (data["operation_id"]!=capture.operation_id or data["id"]!=snapshot["id"] or data["mode"]!=snapshot["mode"]
                or data["customer_id"]!=snapshot["customer_id"]
                or ("occurred_at" in data and data["occurred_at"]!=capture.occurred_at)):raise RecoverySaleSnapshotInvalid()
        if len(data["lines"])!=len(snapshot["lines"]) or len(data["payments"])!=len(snapshot["payments"]):
            raise RecoverySaleSnapshotInvalid()
        amounts=[]
        for requested,frozen in zip(data["lines"],snapshot["lines"],strict=True):
            for key in ("id","product_id","product_version","pack_id","quantity_milli","own_discount_fcfa"):
                if requested[key]!=frozen[key]:raise RecoverySaleSnapshotInvalid()
            base,remainder=divmod(frozen["quantity_milli"]*frozen["factor_milli"],1000)
            if (remainder or base!=frozen["base_quantity_milli"] or base>MAX_FCFA
                    or (frozen["pack_id"] is None and frozen["factor_milli"]!=1000)
                    or (frozen["unit_kind"]=="piece" and base%1000)):raise RecoverySaleSnapshotInvalid()
            reference=line_reference_amount(frozen["quantity_milli"],frozen["unit_price_fcfa"],frozen["unit_kind"])
            if isinstance(reference,PricingFailure) or reference.amount_fcfa!=frozen["reference_fcfa"]:
                raise RecoverySaleSnapshotInvalid()
            amounts.append(LineAmount(frozen["id"],frozen["reference_fcfa"],frozen["own_discount_fcfa"]))
        basket=price_basket(amounts,data["global_discount_fcfa"],None)
        if isinstance(basket,PricingFailure):raise RecoverySaleSnapshotInvalid()
        for frozen,calculated in zip(snapshot["lines"],basket.lines,strict=True):
            if frozen["global_discount_fcfa"]!=calculated.global_discount_fcfa or frozen["net_fcfa"]!=calculated.net_fcfa:
                raise RecoverySaleSnapshotInvalid()
        for key in ("reference_total_fcfa","discount_total_fcfa","net_total_fcfa"):
            if snapshot[key]!=getattr(basket,key):raise RecoverySaleSnapshotInvalid()
        for requested,frozen in zip(data["payments"],snapshot["payments"],strict=True):
            for key in ("id","method","amount_fcfa","other_name","external_reference","received_fcfa","returned_fcfa"):
                if requested.get(key)!=frozen[key]:raise RecoverySaleSnapshotInvalid()
        paid=sum(payment["amount_fcfa"] for payment in data["payments"])
        if (paid>MAX_FCFA or (data["mode"]=="cash" and paid<basket.net_total_fcfa)
                or (data["customer_id"] is None and (data["mode"]=="credit" or paid>basket.net_total_fcfa))):
            raise RecoverySaleSnapshotInvalid()
        expected_due_at=due_at_utc(data["due_date"],timezone_name) if paid<basket.net_total_fcfa else None
        if expected_due_at!=snapshot["due_at"]:raise RecoverySaleSnapshotInvalid()
        canonical=json_value({**data,"installation_id":capture.origin_installation_id})
        return DecodedSale(data,snapshot,basket,canonical)
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):
        raise RecoverySaleSnapshotInvalid() from None


def immutable_receipt_snapshot(receipt):
    """Compare creation facts, excluding mutable balances and warning flags."""
    line_fields=tuple(FrozenSaleLine().fields)
    payment_fields=tuple(FrozenSalePayment().fields)
    debt=receipt.get("debt")
    return {key:receipt[key] for key in ("id","mode","customer_id","reference_total_fcfa","discount_total_fcfa","net_total_fcfa")} | {
        "due_at":debt["due_at"] if debt else None,
        "lines":[{key:row.get(key) for key in line_fields} for row in receipt["lines"]],
        "payments":[{key:row.get(key) for key in payment_fields} for row in receipt["payments"]]}
