"""Recover only version-contiguous contact/deadline changes, without money reset."""
import hashlib
from types import SimpleNamespace
import uuid
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import APIException,ValidationError

from backups.journal_protocol import JournalDateTimeField,canonical_bytes
from backups.services.recovery_customer_snapshots import FrozenCustomerCreationSnapshot,DecodedCustomerOperation,immutable_customer_snapshot
from backups.services.recovery_customers import recorded_origin_evidence
from common.api.fields import ExactIntegerField,StrictSerializer
from common.models import Activity
from common.services.dates import due_at_utc
from common.services.operations import json_value
from customers.api.client_serializers import UpdateClientSerializer
from customers.api.debts import UpdateDebtDueSerializer,CustomerDebtSerializer
from customers.models import Customer,CustomerDebt,CustomerLedgerEntry
from customers.services.client_services import client_receipt
from customers.services.duplicate_queries import possible_duplicates_of
from identity.models import Installation
from sales.services.pricing import MAX_FCFA


class RecoveryCustomerRevisionInvalid(APIException):
    status_code=400
    default_code="recovery_customer_revision_invalid"
    default_detail=_("Recovery customer revision snapshot is inconsistent.")


class FrozenCustomerState(FrozenCustomerCreationSnapshot):
    version=ExactIntegerField(min_value=1,max_value=MAX_FCFA)


class FrozenCustomerUpdateSnapshot(StrictSerializer):
    before=FrozenCustomerState()
    after=FrozenCustomerState()


class FrozenDueChangeSnapshot(StrictSerializer):
    id=serializers.UUIDField()
    customer_id=serializers.UUIDField()
    before_due_at=JournalDateTimeField(allow_null=True)
    after_due_at=JournalDateTimeField(allow_null=True)
    before_version=ExactIntegerField(min_value=1,max_value=MAX_FCFA-1)
    after_version=ExactIntegerField(min_value=2,max_value=MAX_FCFA)
    occurred_at=JournalDateTimeField()


class CustomerUpdatePayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    customer_update=FrozenCustomerUpdateSnapshot()


class DueChangePayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    due_change=FrozenDueChangeSnapshot()


def decode_customer_revision(capture,*,timezone_name):
    try:
        contact=capture.operation_type=="customer_update"
        incoming=(CustomerUpdatePayload if contact else DueChangePayload)(data=capture.payload)
        incoming.is_valid(raise_exception=True)
        snapshot=incoming.validated_data["customer_update" if contact else "due_change"]
        raw=incoming.validated_data["original_request"]
        if not isinstance(raw,dict):raise RecoveryCustomerRevisionInvalid()
        raw=dict(raw)
        if uuid.UUID(str(raw.pop("installation_id",capture.origin_installation_id)))!=capture.origin_installation_id:
            raise RecoveryCustomerRevisionInvalid()
        if contact:
            before,after=snapshot["before"],snapshot["after"]
            if uuid.UUID(str(raw.pop("client_id",after["id"])))!=after["id"]:raise RecoveryCustomerRevisionInvalid()
            request=UpdateClientSerializer(data=raw);request.is_valid(raise_exception=True);data=request.validated_data
            if (before["id"]!=after["id"] or data["base_version"]!=before["version"] or after["version"]!=before["version"]+1
                    or before["created_at"]!=after["created_at"] or after["updated_at"]!=capture.occurred_at
                    or any(after[key]!=data.get(key,before[key]) for key in ("name","phone"))):
                raise RecoveryCustomerRevisionInvalid()
            canonical=json_value({**data,"client_id":after["id"]})
        else:
            if (uuid.UUID(str(raw.pop("debt_id",snapshot["id"])))!=snapshot["id"]
                    or uuid.UUID(str(raw.pop("customer_id",snapshot["customer_id"])))!=snapshot["customer_id"]):
                raise RecoveryCustomerRevisionInvalid()
            request=UpdateDebtDueSerializer(data=raw);request.is_valid(raise_exception=True);data=request.validated_data
            if (data["base_version"]!=snapshot["before_version"] or snapshot["after_version"]!=snapshot["before_version"]+1
                    or due_at_utc(data["due_date"],timezone_name)!=snapshot["after_due_at"] or snapshot["occurred_at"]!=capture.occurred_at):
                raise RecoveryCustomerRevisionInvalid()
            canonical=json_value({**data,"debt_id":snapshot["id"],"installation_id":capture.origin_installation_id})
        if data["operation_id"]!=capture.operation_id:raise RecoveryCustomerRevisionInvalid()
        return DecodedCustomerOperation(data,snapshot,canonical)
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):
        raise RecoveryCustomerRevisionInvalid() from None


def _known_revision(shop,capture,decoded,current,issues,notices):
    operation=Activity.objects.filter(shop=shop,id=capture.operation_id).first()
    if operation is None:
        if Activity.objects.filter(id=capture.operation_id).exists():issues.append({"code":"operation_conflict","ids":[str(capture.operation_id)]})
        return None,False
    verified=False;contact=capture.operation_type=="customer_update";snapshot=decoded.snapshot
    try:
        if not isinstance(operation.details,dict):raise ValueError()
        actor,device,verified,origin=recorded_origin_evidence(capture,operation,notices,client_metadata=contact)
        target=snapshot["after"]["id"] if contact else snapshot["id"]
        matches=(current is not None and operation.target_id==target and actor==capture.origin_user_id and device
            and operation.action==("client_updated" if contact else "customer_debt_due_changed")
            and operation.details.get("request")==decoded.canonical_request)
        if contact:
            matches=matches and (immutable_customer_snapshot(operation.details["before"])==json_value(snapshot["before"])
                and immutable_customer_snapshot(operation.details["response"])==json_value(snapshot["after"])
                and operation.details["before"]["shop_id"]==str(shop.id) and operation.details["response"]["shop_id"]==str(shop.id))
        else:
            receipt=operation.details["response"]
            matches=matches and (operation.details["before_due_at"]==json_value(snapshot["before_due_at"])
                and receipt["id"]==str(snapshot["id"]) and receipt["shop_id"]==str(shop.id)
                and receipt["due_at"]==json_value(snapshot["after_due_at"]) and receipt["due_version"]==snapshot["after_version"]
                and receipt["customer_id"]==str(snapshot["customer_id"])
                and (origin.capture.occurred_at if origin else operation.created_at)==capture.occurred_at)
    except (KeyError,ValueError,TypeError,AttributeError):matches=False
    if not matches:issues.append({"code":"operation_conflict","ids":[str(capture.operation_id)]})
    return operation,verified


def build_customer_revision_preview(shop,capture):
    contact=capture.operation_type=="customer_update"
    decoded=decode_customer_revision(capture,timezone_name=shop.timezone);snapshot=decoded.snapshot
    if contact:
        current=Customer.objects.filter(shop=shop,id=snapshot["before"]["id"]).first()
        state=client_receipt(current) if current else None
        after=snapshot["after"]
        duplicate=possible_duplicates_of(SimpleNamespace(pk=after["id"],shop_id=shop.id,name=after["name"],phone=after["phone"])).exists()
    else:
        current=CustomerDebt.objects.filter(shop=shop,id=snapshot["id"],customer_id=snapshot["customer_id"],customer__shop=shop).select_related("shop","customer").first()
        state=json_value(CustomerDebtSerializer(current).data) if current else None
        duplicate=False
    issues=[];notices=[]
    known,verified=_known_revision(shop,capture,decoded,current,issues,notices)
    if not verified and not notices:notices.append({"code":"declared_origin_installation","ids":[]})
    if known is None:
        if capture.occurred_at>timezone.now():issues.append({"code":"future_effective_date","ids":[]})
        if current is None:issues.append({"code":"missing_customer" if contact else "missing_debt","ids":[str(snapshot["before"]["id"] if contact else snapshot["id"])]})
        elif contact:
            if immutable_customer_snapshot(state)!=json_value(snapshot["before"]):issues.append({"code":"customer_revision_conflict","ids":[str(current.id)]})
        elif current.due_version!=snapshot["before_version"] or current.due_at!=snapshot["before_due_at"]:
            issues.append({"code":"debt_due_revision_conflict","ids":[str(current.id)]})
        ready={str(value) for value in Activity.objects.filter(shop=shop,id__in=capture.dependencies,details__has_key="response").values_list("id",flat=True)}
        missing=sorted(set(capture.dependencies)-ready)
        if missing:issues.append({"code":"missing_dependencies","ids":missing})
        if not contact and CustomerLedgerEntry.objects.filter(id=uuid.uuid5(capture.operation_id,"customer-ledger:due_date_changed")).exists():
            issues.append({"code":"entity_uuid_conflict","ids":[]})
    installation=Installation.objects.filter(id=capture.origin_installation_id).values("user_id").first()
    if installation and installation["user_id"]!=capture.origin_user_id:issues.append({"code":"origin_identity_conflict","ids":[]})
    latest=capture.integration_decisions.order_by("-version").first();version=latest.version if latest else 0
    frame=json_value({"capture_id":capture.id,"source_sha256":capture.content_sha256,"version":version,"timezone":shop.timezone,
        "state":state,"has_possible_duplicate":duplicate,"known_receipt":known.details.get("response") if known and isinstance(known.details,dict) else None,
        "origin_installation_verified":verified,"issues":issues,"notices":notices})
    token=hashlib.sha256(canonical_bytes(frame)).hexdigest()
    result=json_value({"capture_id":capture.id,"operation_type":capture.operation_type,"version":version,"preview_token":token,
        "can_apply":not issues,"outcome":"blocked" if issues else "already_known" if known else "new",
        "hold_id":None,"review_state":None,"review_version":0,"can_hold":False,"can_refuse":False,"snapshot":snapshot,
        "current_state":state,"stocks":[],"cash_delta_by_method_fcfa":{},"new_debt_fcfa":0,"new_customer_credit_fcfa":0,
        "has_possible_duplicate":duplicate,"origin_installation_verified":verified,"notices":notices,"issues":issues,
        "origin_user_id":capture.origin_user_id,"origin_installation_id":capture.origin_installation_id,"origin_recorded_at":capture.origin_recorded_at})
    return result,decoded,known,{},current if contact else current.customer if current else None
