"""Replay an explicit allocation exactly, without selecting debts or moving cash."""
from collections import defaultdict
from dataclasses import dataclass
import hashlib
import uuid

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import APIException,ValidationError
from backups.journal_protocol import JournalDateTimeField,canonical_bytes
from backups.services.recovery_customers import recorded_origin_evidence
from common.api.fields import StrictSerializer,ExactIntegerField
from common.models import Activity
from common.services.operations import json_value
from customers.api.payment_serializers import CreateAllocationSerializer
from customers.api.regularizations import AllocateUnallocatedSerializer
from customers.models import Customer,CustomerPayment,CustomerCredit,CustomerDebt,CustomerLedgerEntry,PaymentRegularization,RegularizationAllocation
from identity.models import Installation
from sales.services.pricing import MAX_FCFA


class RecoveryAllocationSnapshotInvalid(APIException):
    status_code=400
    default_code="recovery_allocation_snapshot_invalid"
    default_detail=_("Recovery customer allocation snapshot is inconsistent.")


class FrozenCustomerAllocation(StrictSerializer):
    id=serializers.UUIDField()
    customer_id=serializers.UUIDField()
    payment_id=serializers.UUIDField(allow_null=True)
    credit_id=serializers.UUIDField(allow_null=True)
    amount_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    before_unallocated_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    after_unallocated_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    occurred_at=JournalDateTimeField()
    allocations=CreateAllocationSerializer(many=True,allow_empty=False,max_length=500)


class CustomerAllocationPayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    allocation=FrozenCustomerAllocation()


@dataclass(frozen=True)
class DecodedCustomerAllocation:
    request:dict
    snapshot:dict
    canonical_request:dict
    source_kind:str


def decode_customer_allocation(capture):
    try:
        incoming=CustomerAllocationPayload(data=capture.payload);incoming.is_valid(raise_exception=True)
        snapshot=incoming.validated_data["allocation"];raw=incoming.validated_data["original_request"]
        if not isinstance(raw,dict) or (snapshot["payment_id"] is None)==(snapshot["credit_id"] is None):
            raise RecoveryAllocationSnapshotInvalid()
        raw=dict(raw);kind="payment" if snapshot["payment_id"] else "credit";source_id=snapshot[kind+"_id"]
        if (raw.pop("kind","allocation")!="allocation"
                or uuid.UUID(str(raw.pop("installation_id",capture.origin_installation_id)))!=capture.origin_installation_id
                or uuid.UUID(str(raw.pop(kind+"_id",source_id)))!=source_id
                or uuid.UUID(str(raw.pop("customer_id",snapshot["customer_id"])))!=snapshot["customer_id"]):
            raise RecoveryAllocationSnapshotInvalid()
        request=AllocateUnallocatedSerializer(data=raw);request.is_valid(raise_exception=True);data=request.validated_data
        if (data["id"]!=snapshot["id"] or data["operation_id"]!=capture.operation_id
                or snapshot["occurred_at"]!=capture.occurred_at or data["allocations"]!=snapshot["allocations"]
                or sum(row["amount_fcfa"] for row in data["allocations"])!=snapshot["amount_fcfa"]
                or snapshot["before_unallocated_fcfa"]-snapshot["amount_fcfa"]!=snapshot["after_unallocated_fcfa"]):
            raise RecoveryAllocationSnapshotInvalid()
        canonical=json_value({**data,kind+"_id":source_id,"kind":"allocation","installation_id":capture.origin_installation_id})
        return DecodedCustomerAllocation(data,snapshot,canonical,kind)
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):
        raise RecoveryAllocationSnapshotInvalid() from None


def _known_allocation(shop,capture,decoded,source,issues,notices):
    operation=Activity.objects.filter(shop=shop,id=capture.operation_id).first()
    if operation is None:
        if Activity.objects.filter(id=capture.operation_id).exists():issues.append({"code":"operation_conflict","ids":[str(capture.operation_id)]})
        return None,False
    snapshot=decoded.snapshot;kind=decoded.source_kind;verified=False
    try:
        if not isinstance(operation.details,dict):raise ValueError()
        actor,device,verified,origin=recorded_origin_evidence(capture,operation,notices,client_metadata=False)
        receipt=operation.details["response"]
        initial=(operation.details["recovery"]["source_snapshot"] if origin else {
            **{key:receipt[key] for key in snapshot if key not in ("customer_id","before_unallocated_fcfa")},
            "customer_id":str(source.customer_id) if source else None,"before_unallocated_fcfa":operation.details["before_unallocated_fcfa"]})
        matches=(source is not None and actor==capture.origin_user_id and device
            and operation.action=="customer_"+kind+"_allocation" and operation.target_id==snapshot[kind+"_id"]
            and operation.details.get("request")==decoded.canonical_request and initial==json_value(snapshot)
            and all(receipt[key]==json_value(snapshot[key]) for key in ("id","payment_id","credit_id","amount_fcfa","occurred_at","allocations"))
            and receipt["actor_id"]==str(capture.origin_user_id) and receipt["reason"]==decoded.request["reason"]
            and receipt["operation_id"]==str(capture.operation_id) and receipt["kind"]=="allocation"
            and not any(receipt[key] for key in ("method","other_name","external_reference"))
            and type(operation.details["before_unallocated_fcfa"]) is int and type(operation.details["after_unallocated_fcfa"]) is int
            and operation.details["before_unallocated_fcfa"]-snapshot["amount_fcfa"]==operation.details["after_unallocated_fcfa"]>=0
            and receipt["after_unallocated_fcfa"]==operation.details["after_unallocated_fcfa"]
            and PaymentRegularization.objects.filter(id=snapshot["id"],operation=operation,kind="allocation",**{kind:source}).exists())
        if matches:
            rows=list(RegularizationAllocation.objects.filter(regularization__operation=operation,
                debt__shop=shop,debt__customer_id=snapshot["customer_id"]).order_by("position","id").values("id","debt_id","amount_fcfa"))
            matches=json_value(rows)==json_value(snapshot["allocations"])
    except (KeyError,TypeError,ValueError,AttributeError):matches=False
    if not matches:issues.append({"code":"operation_conflict","ids":[str(capture.operation_id)]})
    return operation,verified


def build_customer_allocation_preview(shop,capture):
    decoded=decode_customer_allocation(capture);snapshot=decoded.snapshot;kind=decoded.source_kind
    customer=Customer.objects.filter(shop=shop,id=snapshot["customer_id"]).first()
    model=CustomerPayment if kind=="payment" else CustomerCredit
    source=model.objects.filter(shop=shop,customer=customer,id=snapshot[kind+"_id"]).first() if customer else None
    issues=[];notices=[]
    known,verified=_known_allocation(shop,capture,decoded,source,issues,notices)
    totals=defaultdict(int)
    for row in snapshot["allocations"]:totals[row["debt_id"]]+=row["amount_fcfa"]
    debts={row["id"]:row for row in CustomerDebt.objects.filter(shop=shop,customer_id=snapshot["customer_id"],
        customer__shop=shop,id__in=totals).values("id","amount_fcfa","remaining_fcfa","occurred_at")}
    balance=getattr(source,"unallocated_fcfa" if kind=="payment" else "remaining_fcfa") if source else None
    if known is None:
        if capture.occurred_at>timezone.now():issues.append({"code":"future_effective_date","ids":[]})
        if customer is None:issues.append({"code":"missing_customer","ids":[str(snapshot["customer_id"])]})
        if source is None:issues.append({"code":"missing_allocation_source","ids":[str(snapshot[kind+"_id"])]})
        elif capture.occurred_at<source.occurred_at:issues.append({"code":"allocation_precedes_source","ids":[]})
        if source is not None and snapshot["before_unallocated_fcfa"]>source.amount_fcfa:issues.append({"code":"allocation_source_amount_conflict","ids":[]})
        if balance is not None and balance<snapshot["amount_fcfa"]:issues.append({"code":"insufficient_allocation_credit","ids":[]})
        for debt_id in sorted(totals,key=str):
            debt=debts.get(debt_id)
            if debt is None:issues.append({"code":"missing_debt","ids":[str(debt_id)]})
            elif totals[debt_id]>debt["remaining_fcfa"]:issues.append({"code":"allocation_exceeds_debt","ids":[str(debt_id)]})
            if debt and capture.occurred_at<debt["occurred_at"]:issues.append({"code":"allocation_precedes_debt","ids":[str(debt_id)]})
        ready={str(value) for value in Activity.objects.filter(shop=shop,id__in=capture.dependencies,details__has_key="response").values_list("id",flat=True)}
        missing=sorted(set(capture.dependencies)-ready)
        if missing:issues.append({"code":"missing_dependencies","ids":missing})
        if (PaymentRegularization.objects.filter(id=snapshot["id"]).exists()
                or RegularizationAllocation.objects.filter(id__in=[row["id"] for row in snapshot["allocations"]]).exists()
                or CustomerLedgerEntry.objects.filter(id=uuid.uuid5(snapshot["id"],"customer-ledger:allocation")).exists()):
            issues.append({"code":"entity_uuid_conflict","ids":[]})
    installation=Installation.objects.filter(id=capture.origin_installation_id).values("user_id").first()
    if installation and installation["user_id"]!=capture.origin_user_id:issues.append({"code":"origin_identity_conflict","ids":[]})
    if not verified and not notices:notices.append({"code":"declared_origin_installation","ids":[]})
    latest=capture.integration_decisions.order_by("-version").first();version=latest.version if latest else 0
    state=None if source is None else {"source_kind":kind,"id":source.id,"customer_id":source.customer_id,
        "amount_fcfa":source.amount_fcfa,"available_credit_fcfa":balance,"occurred_at":source.occurred_at}
    impacts=[]
    for debt_id,total in sorted(totals.items(),key=lambda row:str(row[0])):
        before=debts[debt_id]["remaining_fcfa"] if debt_id in debts else None
        after=before if known else before-total if before is not None and before>=total else None
        impacts.append({"debt_id":debt_id,"requested_fcfa":total,"remaining_before_fcfa":before,
            "remaining_after_fcfa":after,"can_apply":known is not None or before is not None and before>=total})
    frame=json_value({"capture_id":capture.id,"source_sha256":capture.content_sha256,"version":version,"state":state,
        "customer":None if customer is None else {"id":customer.id,"version":customer.version},"issues":issues,"notices":notices,
        "debts":sorted(debts.values(),key=lambda row:str(row["id"])),"origin_installation_verified":verified,
        "known_receipt":known.details.get("response") if known and isinstance(known.details,dict) else None})
    token=hashlib.sha256(canonical_bytes(frame)).hexdigest()
    preview=json_value({"capture_id":capture.id,"operation_type":"customer_allocation","version":version,"preview_token":token,
        "can_apply":not issues,"outcome":"blocked" if issues else "already_known" if known else "new",
        "hold_id":None,"review_state":None,"review_version":0,"can_hold":False,"can_refuse":False,"snapshot":snapshot,
        "current_state":state,"debt_allocation_impacts":impacts,"stocks":[],"cash_delta_by_method_fcfa":{},
        "new_debt_fcfa":0,"new_customer_credit_fcfa":0,"origin_installation_verified":verified,"notices":notices,"issues":issues,
        "origin_user_id":capture.origin_user_id,"origin_installation_id":capture.origin_installation_id,"origin_recorded_at":capture.origin_recorded_at})
    return preview,decoded,known,{},customer
