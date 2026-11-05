"""Recover an exact real refund without recreating a payment or source rights."""
from dataclasses import dataclass
import hashlib
import uuid

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import APIException,ValidationError
from backups.journal_protocol import JournalDateTimeField,canonical_bytes
from backups.services.recovery_customers import recorded_origin_evidence
from common.api.fields import ExactIntegerField,StrictSerializer
from common.models import Activity
from common.services.operations import json_value
from customers.api.regularizations import RefundUnallocatedSerializer
from customers.models import Customer,CustomerCredit,CustomerPayment,PaymentRegularization,CustomerLedgerEntry
from identity.models import Installation
from sales.api.sale_serializers import DeclaredPaymentSerializer
from sales.services.pricing import MAX_FCFA


class RecoveryRefundSnapshotInvalid(APIException):
    status_code=400
    default_code="recovery_refund_snapshot_invalid"
    default_detail=_("Recovery customer refund snapshot is inconsistent.")


class FrozenCustomerRefund(DeclaredPaymentSerializer):
    customer_id=serializers.UUIDField()
    payment_id=serializers.UUIDField(allow_null=True)
    credit_id=serializers.UUIDField(allow_null=True)
    before_unallocated_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    after_unallocated_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    occurred_at=JournalDateTimeField()


class CustomerRefundPayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    refund=FrozenCustomerRefund()


@dataclass(frozen=True)
class DecodedCustomerRefund:
    request:dict
    snapshot:dict
    canonical_request:dict
    source_kind:str


def decode_customer_refund(capture):
    try:
        incoming=CustomerRefundPayload(data=capture.payload);incoming.is_valid(raise_exception=True)
        snapshot=incoming.validated_data["refund"];raw=incoming.validated_data["original_request"]
        if not isinstance(raw,dict) or (snapshot["payment_id"] is None)==(snapshot["credit_id"] is None):
            raise RecoveryRefundSnapshotInvalid()
        raw=dict(raw);kind="payment" if snapshot["payment_id"] else "credit";source_id=snapshot[kind+"_id"]
        if (raw.pop("kind","refund")!="refund"
                or uuid.UUID(str(raw.pop("installation_id",capture.origin_installation_id)))!=capture.origin_installation_id
                or uuid.UUID(str(raw.pop(kind+"_id",source_id)))!=source_id
                or uuid.UUID(str(raw.pop("customer_id",snapshot["customer_id"])))!=snapshot["customer_id"]):
            raise RecoveryRefundSnapshotInvalid()
        request=RefundUnallocatedSerializer(data=raw);request.is_valid(raise_exception=True);data=request.validated_data
        if (data["operation_id"]!=capture.operation_id or snapshot["occurred_at"]!=capture.occurred_at
                or snapshot["before_unallocated_fcfa"]-snapshot["amount_fcfa"]!=snapshot["after_unallocated_fcfa"]
                or any(data[key]!=snapshot[key] for key in ("id","amount_fcfa","method","other_name","external_reference"))):
            raise RecoveryRefundSnapshotInvalid()
        canonical=json_value({**data,kind+"_id":source_id,"kind":"refund","installation_id":capture.origin_installation_id})
        return DecodedCustomerRefund(data,snapshot,canonical,kind)
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):
        raise RecoveryRefundSnapshotInvalid() from None


def _known_refund(shop,capture,decoded,source,issues,notices):
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
        matches=(source is not None and operation.action=="customer_"+kind+"_refund"
            and operation.target_id==snapshot[kind+"_id"] and actor==capture.origin_user_id and device
            and operation.details.get("request")==decoded.canonical_request and initial==json_value(snapshot)
            and all(receipt[key]==json_value(snapshot[key]) for key in ("id","payment_id","credit_id","amount_fcfa","method","other_name","external_reference","occurred_at"))
            and receipt["actor_id"]==str(capture.origin_user_id) and receipt["reason"]==decoded.request["reason"]
            and type(operation.details["before_unallocated_fcfa"]) is int and type(operation.details["after_unallocated_fcfa"]) is int
            and operation.details["before_unallocated_fcfa"]-snapshot["amount_fcfa"]==operation.details["after_unallocated_fcfa"]>=0
            and receipt["after_unallocated_fcfa"]==operation.details["after_unallocated_fcfa"]
            and receipt["operation_id"]==str(capture.operation_id) and receipt["kind"]=="refund" and receipt["allocations"]==[]
            and PaymentRegularization.objects.filter(id=snapshot["id"],operation=operation,**{kind:source}).exists())
    except (KeyError,TypeError,ValueError,AttributeError):matches=False
    if not matches:issues.append({"code":"operation_conflict","ids":[str(capture.operation_id)]})
    return operation,verified


def build_customer_refund_preview(shop,capture):
    decoded=decode_customer_refund(capture);snapshot=decoded.snapshot;kind=decoded.source_kind
    customer=Customer.objects.filter(shop=shop,id=snapshot["customer_id"]).first()
    model=CustomerPayment if kind=="payment" else CustomerCredit
    source=model.objects.filter(shop=shop,customer=customer,id=snapshot[kind+"_id"]).first() if customer else None
    issues=[];notices=[]
    known,verified=_known_refund(shop,capture,decoded,source,issues,notices)
    balance=getattr(source,"unallocated_fcfa" if kind=="payment" else "remaining_fcfa") if source else None
    if known is None:
        if capture.occurred_at>timezone.now():issues.append({"code":"future_effective_date","ids":[]})
        if customer is None:issues.append({"code":"missing_customer","ids":[str(snapshot["customer_id"])]})
        if source is None:issues.append({"code":"missing_refund_source","ids":[str(snapshot[kind+"_id"])]})
        elif capture.occurred_at<source.occurred_at:issues.append({"code":"refund_precedes_source","ids":[]})
        if source is not None and snapshot["before_unallocated_fcfa"]>source.amount_fcfa:
            issues.append({"code":"refund_source_amount_conflict","ids":[]})
        if balance is not None and balance<snapshot["amount_fcfa"]:issues.append({"code":"insufficient_refund_credit","ids":[]})
        ready={str(value) for value in Activity.objects.filter(shop=shop,id__in=capture.dependencies,details__has_key="response").values_list("id",flat=True)}
        missing=sorted(set(capture.dependencies)-ready)
        if missing:issues.append({"code":"missing_dependencies","ids":missing})
        if (PaymentRegularization.objects.filter(id=snapshot["id"]).exists()
                or CustomerLedgerEntry.objects.filter(id=uuid.uuid5(snapshot["id"],"customer-ledger:refund")).exists()):
            issues.append({"code":"entity_uuid_conflict","ids":[]})
    installation=Installation.objects.filter(id=capture.origin_installation_id).values("user_id").first()
    if installation and installation["user_id"]!=capture.origin_user_id:issues.append({"code":"origin_identity_conflict","ids":[]})
    if not verified and not notices:notices.append({"code":"declared_origin_installation","ids":[]})
    latest=capture.integration_decisions.order_by("-version").first();version=latest.version if latest else 0
    state=None if source is None else {"source_kind":kind,"id":source.id,"customer_id":source.customer_id,
        "amount_fcfa":source.amount_fcfa,"available_credit_fcfa":balance,"occurred_at":source.occurred_at}
    frame=json_value({"capture_id":capture.id,"source_sha256":capture.content_sha256,"version":version,"state":state,
        "customer":None if customer is None else {"id":customer.id,"version":customer.version},"issues":issues,"notices":notices,
        "origin_installation_verified":verified,"known_receipt":known.details.get("response") if known and isinstance(known.details,dict) else None})
    token=hashlib.sha256(canonical_bytes(frame)).hexdigest()
    key="other:"+snapshot["other_name"] if snapshot["method"]=="other" else snapshot["method"]
    preview=json_value({"capture_id":capture.id,"operation_type":"customer_refund","version":version,"preview_token":token,
        "can_apply":not issues,"outcome":"blocked" if issues else "already_known" if known else "new",
        "hold_id":None,"review_state":None,"review_version":0,"can_hold":False,"can_refuse":False,"snapshot":snapshot,
        "current_state":state,"stocks":[],"cash_delta_by_method_fcfa":{} if known else {key:-snapshot["amount_fcfa"]},
        "new_debt_fcfa":0,"new_customer_credit_fcfa":0,"origin_installation_verified":verified,"notices":notices,"issues":issues,
        "origin_user_id":capture.origin_user_id,"origin_installation_id":capture.origin_installation_id,"origin_recorded_at":capture.origin_recorded_at})
    return preview,decoded,known,{},customer
