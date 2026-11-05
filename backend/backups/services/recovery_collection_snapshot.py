"""Validate immutable initial collection facts independently from current debts."""
from collections import defaultdict
from dataclasses import dataclass
import uuid
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import APIException,ValidationError
from common.api.fields import ExactIntegerField,StrictSerializer,StrictBoolean
from common.services.operations import json_value
from customers.api.payment_serializers import CreateCustomerPaymentSerializer
from backups.services.recovery_sale_snapshot import FrozenSalePayment
from sales.services.pricing import MAX_FCFA


class RecoveryCollectionSnapshotInvalid(APIException):
    status_code=400
    default_code="recovery_collection_snapshot_invalid"
    default_detail=_("Recovery collection snapshot is inconsistent.")


class FrozenCollectionAllocation(StrictSerializer):
    id=serializers.UUIDField()
    debt_id=serializers.UUIDField()
    requested_amount_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    applied_amount_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)


class FrozenCollectionSnapshot(StrictSerializer):
    id=serializers.UUIDField()
    customer_id=serializers.UUIDField()
    amount_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    unallocated_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    requires_regularization=StrictBoolean()
    parts=FrozenSalePayment(many=True,allow_empty=False,max_length=50)
    allocations=FrozenCollectionAllocation(many=True,max_length=500)


class FrozenCollectionPayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    collection=FrozenCollectionSnapshot()


@dataclass(frozen=True)
class DecodedCollection:
    request:dict
    snapshot:dict
    canonical_request:dict


def decode_collection_snapshot(capture):
    try:
        incoming=FrozenCollectionPayload(data=capture.payload);incoming.is_valid(raise_exception=True)
        snapshot=incoming.validated_data["collection"]
        raw=incoming.validated_data["original_request"]
        if not isinstance(raw,dict):raise RecoveryCollectionSnapshotInvalid()
        raw=dict(raw)
        if (uuid.UUID(str(raw.pop("installation_id",capture.origin_installation_id)))!=capture.origin_installation_id
                or uuid.UUID(str(raw.pop("customer_id",snapshot["customer_id"])))!=snapshot["customer_id"]):
            raise RecoveryCollectionSnapshotInvalid()
        request=CreateCustomerPaymentSerializer(data=raw);request.is_valid(raise_exception=True)
        data=request.validated_data
        if (data["operation_id"]!=capture.operation_id or data["id"]!=snapshot["id"]
                or len(data["parts"])!=len(snapshot["parts"]) or len(data["allocations"])!=len(snapshot["allocations"])):
            raise RecoveryCollectionSnapshotInvalid()
        total=sum(row["amount_fcfa"] for row in data["parts"])
        applied=sum(row["applied_amount_fcfa"] for row in snapshot["allocations"])
        if (snapshot["amount_fcfa"]!=total or snapshot["unallocated_fcfa"]!=total-applied
                or snapshot["requires_regularization"]!=(snapshot["unallocated_fcfa"]>0)):
            raise RecoveryCollectionSnapshotInvalid()
        for asked,frozen in zip(data["parts"],snapshot["parts"],strict=True):
            if any(asked.get(key)!=frozen[key] for key in ("id","method","amount_fcfa","other_name","external_reference","received_fcfa","returned_fcfa")):
                raise RecoveryCollectionSnapshotInvalid()
        totals=defaultdict(int)
        for asked,frozen in zip(data["allocations"],snapshot["allocations"],strict=True):
            if (asked["id"]!=frozen["id"] or asked["debt_id"]!=frozen["debt_id"]
                    or asked["amount_fcfa"]!=frozen["requested_amount_fcfa"]
                    or frozen["applied_amount_fcfa"]>frozen["requested_amount_fcfa"]):
                raise RecoveryCollectionSnapshotInvalid()
            totals[frozen["debt_id"]]+=frozen["applied_amount_fcfa"]
        # A repeated debt cannot receive more after an earlier request exhausted
        # it. Reconstruct the smallest possible original balance per debt.
        for frozen in snapshot["allocations"]:
            available=totals[frozen["debt_id"]]
            if frozen["applied_amount_fcfa"]!=min(available,frozen["requested_amount_fcfa"]):raise RecoveryCollectionSnapshotInvalid()
            totals[frozen["debt_id"]]-=frozen["applied_amount_fcfa"]
        canonical=json_value({**data,"customer_id":snapshot["customer_id"],"installation_id":capture.origin_installation_id})
        return DecodedCollection(data,snapshot,canonical)
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):
        raise RecoveryCollectionSnapshotInvalid() from None


def immutable_collection_receipt(receipt):
    fields=tuple(FrozenCollectionSnapshot().fields)
    part_fields=tuple(FrozenSalePayment().fields)
    allocation_fields=tuple(FrozenCollectionAllocation().fields)
    return {key:receipt[key] for key in fields if key not in ("parts","allocations")} | {
        "parts":[{key:row.get(key) for key in part_fields} for row in receipt["parts"]],
        "allocations":[{key:row[key] for key in allocation_fields} for row in receipt["allocations"]]}
