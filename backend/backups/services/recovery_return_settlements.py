"""Recover explicit uses of a return claim without replaying the physical return."""
from dataclasses import dataclass
import hashlib
import uuid
from django.db.models import Q
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
from customers.api.return_credit_settlements import ReturnCreditRefundInput,ReturnCreditAllocationInput
from customers.models import ReturnCreditSettlement,ReturnCreditAllocation,CustomerDebt,CustomerLedgerEntry
from customers.services.return_credit_balance import with_return_credit,remaining_return_credit
from identity.models import Installation
from sales.models import SaleReturn,SalePayment
from sales.services.pricing import MAX_FCFA


class RecoveryReturnSettlementInvalid(APIException):
    status_code=400
    default_code="recovery_return_settlement_invalid"
    default_detail=_("Recovery return credit settlement snapshot is inconsistent.")


class FrozenReturnSettlement(StrictSerializer):
    id=serializers.UUIDField()
    sale_return_id=serializers.UUIDField()
    sale_id=serializers.UUIDField()
    customer_id=serializers.UUIDField(allow_null=True)
    kind=serializers.ChoiceField(choices=["refund","allocation"])
    amount_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    before_credit_fcfa=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    after_credit_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    occurred_at=JournalDateTimeField()
    created_at=JournalDateTimeField()
    method=serializers.ChoiceField(choices=SalePayment.Method.choices,allow_blank=True)
    other_name=serializers.CharField(max_length=80,allow_blank=True,trim_whitespace=False)
    external_reference=serializers.CharField(max_length=150,allow_blank=True,trim_whitespace=False)
    allocations=CreateAllocationSerializer(many=True,max_length=500)


class ReturnSettlementPayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    settlement=FrozenReturnSettlement()


class SourceReturnRefund(ReturnCreditRefundInput):
    occurred_at=JournalDateTimeField()
    def validate_occurred_at(self,value):return value


class SourceReturnAllocation(ReturnCreditAllocationInput):
    occurred_at=JournalDateTimeField()
    def validate_occurred_at(self,value):return value


@dataclass(frozen=True)
class DecodedReturnSettlement:
    request:dict
    snapshot:dict
    canonical_request:dict


def decode_return_settlement(capture):
    try:
        incoming=ReturnSettlementPayload(data=capture.payload);incoming.is_valid(raise_exception=True)
        snapshot=incoming.validated_data["settlement"];raw=incoming.validated_data["original_request"]
        if not isinstance(raw,dict):raise RecoveryReturnSettlementInvalid()
        raw=dict(raw);kind=snapshot["kind"]
        if (raw.pop("kind",kind)!=kind
                or uuid.UUID(str(raw.pop("installation_id",capture.origin_installation_id)))!=capture.origin_installation_id
                or uuid.UUID(str(raw.pop("sale_return_id",snapshot["sale_return_id"])))!=snapshot["sale_return_id"]
                or uuid.UUID(str(raw.pop("sale_id",snapshot["sale_id"])))!=snapshot["sale_id"]):
            raise RecoveryReturnSettlementInvalid()
        customer=raw.pop("customer_id",snapshot["customer_id"])
        if (uuid.UUID(str(customer)) if customer is not None else None)!=snapshot["customer_id"]:raise RecoveryReturnSettlementInvalid()
        request=(SourceReturnRefund if kind=="refund" else SourceReturnAllocation)(data=raw)
        request.is_valid(raise_exception=True);data=request.validated_data
        if (data["id"]!=snapshot["id"] or data["operation_id"]!=capture.operation_id
                or data["occurred_at"]!=capture.occurred_at or snapshot["occurred_at"]!=capture.occurred_at
                or snapshot["before_credit_fcfa"]-snapshot["amount_fcfa"]!=snapshot["after_credit_fcfa"]):
            raise RecoveryReturnSettlementInvalid()
        if kind=="refund":
            if snapshot["allocations"] or any(data[key]!=snapshot[key] for key in ("amount_fcfa","method","other_name","external_reference")):
                raise RecoveryReturnSettlementInvalid()
        elif (sum(row["amount_fcfa"] for row in data["allocations"])!=snapshot["amount_fcfa"]
                or sorted(data["allocations"],key=lambda row:str(row["id"]))!=sorted(snapshot["allocations"],key=lambda row:str(row["id"]))
                or any(snapshot[key]!="" for key in ("method","other_name","external_reference"))):
            raise RecoveryReturnSettlementInvalid()
        canonical=json_value({**data,"sale_return_id":snapshot["sale_return_id"],"kind":kind,"installation_id":capture.origin_installation_id})
        return DecodedReturnSettlement(data,snapshot,canonical)
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):
        raise RecoveryReturnSettlementInvalid() from None


def _sorted_allocations(rows):return sorted(json_value(rows),key=lambda row:row["id"])


def _known_settlement(shop,capture,decoded,returned,issues,notices):
    operation=Activity.objects.filter(shop=shop,id=capture.operation_id).first()
    if operation is None:
        if Activity.objects.filter(id=capture.operation_id).exists():issues.append({"code":"operation_conflict","ids":[str(capture.operation_id)]})
        return None,False
    snapshot=decoded.snapshot;verified=False
    try:
        if not isinstance(operation.details,dict):raise ValueError()
        actor,device,verified,origin=recorded_origin_evidence(capture,operation,notices,client_metadata=False)
        receipt=operation.details["response"]
        initial=(operation.details["recovery"]["source_snapshot"] if origin else {
            **{key:receipt[key] for key in snapshot if key not in ("sale_id","customer_id","before_credit_fcfa")},
            "sale_id":str(returned.sale_id) if returned else None,
            "customer_id":str(returned.sale.customer_id) if returned and returned.sale.customer_id else None,
            "before_credit_fcfa":operation.details["before_credit_fcfa"]})
        stored=ReturnCreditSettlement.objects.filter(id=snapshot["id"],operation=operation,sale_return=returned,kind=snapshot["kind"]).first() if returned else None
        matches=(stored is not None and actor==capture.origin_user_id and device
            and operation.action=="return_credit_"+snapshot["kind"] and operation.target_id==snapshot["id"]
            and operation.details.get("request")==decoded.canonical_request and initial==json_value(snapshot)
            and all(receipt[key]==json_value(snapshot[key]) for key in ("id","sale_return_id","kind","amount_fcfa","occurred_at","method","other_name","external_reference"))
            and receipt["operation_id"]==str(capture.operation_id) and receipt["actor_id"]==str(capture.origin_user_id)
            and receipt["reason"]==decoded.request["reason"] and receipt["created_at"]==json_value(stored.created_at)
            and type(operation.details["before_credit_fcfa"]) is int and type(operation.details["after_credit_fcfa"]) is int
            and operation.details["before_credit_fcfa"]-snapshot["amount_fcfa"]==operation.details["after_credit_fcfa"]>=0
            and receipt["after_credit_fcfa"]==operation.details["after_credit_fcfa"]
            and _sorted_allocations(receipt["allocations"])==_sorted_allocations(snapshot["allocations"]))
        if matches:
            rows=list(ReturnCreditAllocation.objects.filter(settlement=stored,debt__shop=shop,
                debt__customer_id=snapshot["customer_id"]).values("id","debt_id","amount_fcfa"))
            matches=_sorted_allocations(rows)==_sorted_allocations(snapshot["allocations"])
    except (KeyError,TypeError,ValueError,AttributeError):matches=False
    if not matches:issues.append({"code":"operation_conflict","ids":[str(capture.operation_id)]})
    return operation,verified


def build_return_settlement_preview(shop,capture):
    decoded=decode_return_settlement(capture);snapshot=decoded.snapshot;kind=snapshot["kind"]
    returned=with_return_credit(SaleReturn.objects.filter(id=snapshot["sale_return_id"],sale_id=snapshot["sale_id"],sale__shop=shop)
        .filter(Q(sale__customer__isnull=True)|Q(sale__customer__shop=shop))).select_related("sale__customer").first()
    issues=[];notices=[]
    known,verified=_known_settlement(shop,capture,decoded,returned,issues,notices)
    available=remaining_return_credit(returned) if returned else None
    targets={row["debt_id"]:row["amount_fcfa"] for row in snapshot["allocations"]}
    debts={row["id"]:row for row in CustomerDebt.objects.filter(shop=shop,customer_id=snapshot["customer_id"],customer__shop=shop,
        id__in=targets).values("id","amount_fcfa","remaining_fcfa","occurred_at")}
    if known is None:
        if capture.occurred_at>timezone.now() or snapshot["created_at"]>timezone.now():issues.append({"code":"future_effective_date","ids":[]})
        if returned is None:issues.append({"code":"missing_return","ids":[str(snapshot["sale_return_id"])]})
        else:
            if returned.sale.customer_id!=snapshot["customer_id"]:issues.append({"code":"return_customer_conflict","ids":[]})
            if capture.occurred_at<returned.occurred_at:issues.append({"code":"settlement_precedes_return","ids":[]})
            if snapshot["before_credit_fcfa"]>returned.credit_fcfa:issues.append({"code":"return_credit_amount_conflict","ids":[]})
            if snapshot["amount_fcfa"]>available:issues.append({"code":"insufficient_return_credit","ids":[]})
        if kind=="allocation" and snapshot["customer_id"] is None:issues.append({"code":"allocation_customer_required","ids":[]})
        for debt_id in sorted(targets,key=str):
            debt=debts.get(debt_id)
            if debt is None:issues.append({"code":"missing_debt","ids":[str(debt_id)]})
            elif targets[debt_id]>debt["remaining_fcfa"]:issues.append({"code":"allocation_exceeds_debt","ids":[str(debt_id)]})
            if debt and capture.occurred_at<debt["occurred_at"]:issues.append({"code":"allocation_precedes_debt","ids":[str(debt_id)]})
        ready={str(value) for value in Activity.objects.filter(shop=shop,id__in=capture.dependencies,details__has_key="response").values_list("id",flat=True)}
        missing=sorted(set(capture.dependencies)-ready)
        if missing:issues.append({"code":"missing_dependencies","ids":missing})
        if (ReturnCreditSettlement.objects.filter(id=snapshot["id"]).exists()
                or ReturnCreditAllocation.objects.filter(id__in=[row["id"] for row in snapshot["allocations"]]).exists()
                or CustomerLedgerEntry.objects.filter(id=uuid.uuid5(snapshot["id"],"customer-ledger:return_credit_"+kind)).exists()):
            issues.append({"code":"entity_uuid_conflict","ids":[]})
    installation=Installation.objects.filter(id=capture.origin_installation_id).values("user_id").first()
    if installation and installation["user_id"]!=capture.origin_user_id:issues.append({"code":"origin_identity_conflict","ids":[]})
    if not verified and not notices:notices.append({"code":"declared_origin_installation","ids":[]})
    latest=capture.integration_decisions.order_by("-version").first();version=latest.version if latest else 0
    state=None if returned is None else {"sale_return_id":returned.id,"sale_id":returned.sale_id,"customer_id":returned.sale.customer_id,
        "amount_fcfa":returned.credit_fcfa,"available_credit_fcfa":available,"occurred_at":returned.occurred_at}
    impacts=[]
    for debt_id,amount in sorted(targets.items(),key=lambda row:str(row[0])):
        before=debts[debt_id]["remaining_fcfa"] if debt_id in debts else None
        impacts.append({"debt_id":debt_id,"requested_fcfa":amount,"remaining_before_fcfa":before,
            "remaining_after_fcfa":before if known else before-amount if before is not None and before>=amount else None,
            "can_apply":known is not None or before is not None and before>=amount})
    frame=json_value({"capture_id":capture.id,"source_sha256":capture.content_sha256,"version":version,"state":state,
        "debts":sorted(debts.values(),key=lambda row:str(row["id"])),"issues":issues,"notices":notices,
        "origin_installation_verified":verified,"known_receipt":known.details.get("response") if known and isinstance(known.details,dict) else None})
    key="other:"+snapshot["other_name"] if snapshot["method"]=="other" else snapshot["method"]
    preview=json_value({"capture_id":capture.id,"operation_type":"return_credit_settlement","version":version,
        "preview_token":hashlib.sha256(canonical_bytes(frame)).hexdigest(),"can_apply":not issues,
        "outcome":"blocked" if issues else "already_known" if known else "new","hold_id":None,"review_state":None,
        "review_version":0,"can_hold":False,"can_refuse":False,"snapshot":snapshot,"current_state":state,"stocks":[],
        "debt_allocation_impacts":impacts,"cash_delta_by_method_fcfa":{key:-snapshot["amount_fcfa"]} if kind=="refund" and known is None else {},
        "new_debt_fcfa":0,"new_customer_credit_fcfa":0,"origin_installation_verified":verified,"issues":issues,"notices":notices,
        "origin_user_id":capture.origin_user_id,"origin_installation_id":capture.origin_installation_id,"origin_recorded_at":capture.origin_recorded_at})
    return preview,decoded,known,{},returned.sale.customer if returned and returned.sale.customer_id else None
