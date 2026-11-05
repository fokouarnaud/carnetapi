"""Preview missing customer dependencies without merging or rewriting history."""
import hashlib
from types import SimpleNamespace
import uuid
from django.utils import timezone

from backups.journal_protocol import canonical_bytes
from backups.models import RecoveredOperationOrigin
from backups.services.recovery_customer_snapshots import decode_customer_creation,decode_opening_debt,immutable_customer_snapshot,immutable_opening_debt_snapshot
from common.models import Activity
from common.services.operations import json_value
from customers.models import Customer,CustomerDebt,CustomerLedgerEntry
from customers.services.duplicate_queries import possible_duplicates_of
from identity.models import Installation


def recorded_origin_evidence(capture,operation,notices,*,client_metadata):
    """Check recorded identity only on the caller's already scoped activity."""
    origin=(RecoveredOperationOrigin.objects.select_related("capture").filter(operation=operation,capture__shop_id=capture.shop_id)
        .defer("capture__payload","capture__dependencies").first())
    if origin is None and RecoveredOperationOrigin.objects.filter(operation=operation).exists():raise ValueError()
    actor_id=origin.capture.origin_user_id if origin else operation.actor_id
    verified=False
    if origin:
        device_matches=(origin.capture.origin_installation_id==capture.origin_installation_id
            and origin.capture.operation_id==capture.operation_id and origin.capture.operation_type==capture.operation_type
            and origin.capture.shop_id==capture.shop_id and origin.capture.occurred_at==capture.occurred_at)
        verified=operation.details["recovery"].get("origin_installation_verified",False) is True
    elif client_metadata and "installation_id" not in operation.details:
        device_matches=True
        notices.append({"code":"legacy_origin_installation_unknown","ids":[]})
    else:
        stored=(operation.details["installation_id"] if client_metadata else operation.details["request"]["installation_id"])
        device_matches=uuid.UUID(str(stored))==capture.origin_installation_id
        verified=device_matches
    return actor_id,device_matches,verified,origin


def _known_customer_operation(capture,decoded,issues,notices):
    operation=Activity.objects.filter(id=capture.operation_id,shop_id=capture.shop_id).first()
    if operation is None:
        if Activity.objects.filter(id=capture.operation_id).exists():
            issues.append({"code":"operation_conflict","ids":[str(capture.operation_id)]})
        return None,False
    is_customer=capture.operation_type=="customer_creation"
    verified=False
    try:
        if not isinstance(operation.details,dict):raise ValueError()
        receipt=operation.details.get("response")
        actor_id,device_matches,verified,_origin=recorded_origin_evidence(capture,operation,notices,client_metadata=is_customer)
        immutable=immutable_customer_snapshot if is_customer else immutable_opening_debt_snapshot
        matches=(operation.shop_id==capture.shop_id and actor_id==capture.origin_user_id and device_matches
            and operation.action==("client_created" if is_customer else "customer_debt_opened")
            and operation.target_id==decoded.snapshot["id"] and operation.details.get("request")==decoded.canonical_request
            and immutable(receipt)==json_value(decoded.snapshot))
        if is_customer:
            matches=matches and Customer.objects.filter(id=decoded.snapshot["id"],shop_id=capture.shop_id).exists()
        else:
            matches=matches and CustomerDebt.objects.filter(id=decoded.snapshot["id"],operation=operation,
                customer_id=decoded.snapshot["customer_id"],shop_id=capture.shop_id,sale__isnull=True).exists()
    except (KeyError,TypeError,ValueError,AttributeError):matches=False
    if not matches:issues.append({"code":"operation_conflict","ids":[str(capture.operation_id)]})
    return operation,verified


def build_customer_operation_preview(shop,capture):
    is_customer=capture.operation_type=="customer_creation"
    decoded=decode_customer_creation(capture) if is_customer else decode_opening_debt(capture,timezone_name=shop.timezone)
    snapshot=decoded.snapshot;issues=[];notices=[]
    known,verified=_known_customer_operation(capture,decoded,issues,notices)
    if not verified and not notices:notices.append({"code":"declared_origin_installation","ids":[]})
    latest=capture.integration_decisions.order_by("-version").first();version=latest.version if latest else 0
    customer=(Customer.objects.filter(shop=shop,id=snapshot["customer_id"]).first() if not is_customer else None)
    duplicate=(possible_duplicates_of(SimpleNamespace(pk=snapshot["id"],shop_id=shop.id,name=snapshot["name"],phone=snapshot["phone"])).exists()
        if is_customer else False)
    if known is None:
        if capture.occurred_at>timezone.now():issues.append({"code":"future_effective_date","ids":[]})
        if is_customer:
            if snapshot["updated_at"]>timezone.now():issues.append({"code":"future_creation_update_date","ids":[]})
            if Customer.objects.filter(id=snapshot["id"]).exists():issues.append({"code":"entity_uuid_conflict","ids":[]})
        else:
            if customer is None:issues.append({"code":"missing_customer","ids":[str(snapshot["customer_id"])]})
            if (CustomerDebt.objects.filter(id=snapshot["id"]).exists()
                    or CustomerLedgerEntry.objects.filter(id=uuid.uuid5(snapshot["id"],"customer-ledger:opening_debt")).exists()):
                issues.append({"code":"entity_uuid_conflict","ids":[]})
        ready={str(value) for value in Activity.objects.filter(shop=shop,id__in=capture.dependencies,
            details__has_key="response").values_list("id",flat=True)}
        missing=sorted(set(capture.dependencies)-ready)
        if missing:issues.append({"code":"missing_dependencies","ids":missing})
    source_device=Installation.objects.filter(id=capture.origin_installation_id).values("user_id").first()
    if source_device is not None and source_device["user_id"]!=capture.origin_user_id:
        issues.append({"code":"origin_identity_conflict","ids":[]})
    frame=json_value({"capture_id":capture.id,"source_sha256":capture.content_sha256,"version":version,"timezone":shop.timezone,
        "known_operation":known.id if known else None,"known_receipt":known.details.get("response") if known and isinstance(known.details,dict) else None,
        "origin_installation_verified":verified,"notices":notices,"issues":issues,"has_possible_duplicate":duplicate,
        "customer":None if customer is None else {"id":customer.id,"version":customer.version}})
    token=hashlib.sha256(canonical_bytes(frame)).hexdigest()
    preview=json_value({"capture_id":capture.id,"operation_type":capture.operation_type,"version":version,"preview_token":token,
        "can_apply":not issues,"outcome":"blocked" if issues else "already_known" if known else "new",
        "hold_id":None,"review_state":None,"review_version":0,"can_hold":False,"can_refuse":False,
        "snapshot":snapshot,"stocks":[],"cash_delta_by_method_fcfa":{},
        "new_debt_fcfa":snapshot["amount_fcfa"] if not is_customer and known is None else 0,"new_customer_credit_fcfa":0,
        "has_possible_duplicate":duplicate,"origin_installation_verified":verified,"notices":notices,
        "origin_user_id":capture.origin_user_id,"origin_installation_id":capture.origin_installation_id,
        "origin_recorded_at":capture.origin_recorded_at,"issues":issues})
    return preview,decoded,known,{},customer
