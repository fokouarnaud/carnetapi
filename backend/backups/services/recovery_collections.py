"""Preview recovered collections against scoped debts without changing source facts."""
import hashlib

from django.utils import timezone

from backups.journal_protocol import canonical_bytes
from backups.models import RecoveredOperationOrigin,RecoveryCollectionReview
from backups.services.recovery_collection_snapshot import decode_collection_snapshot,immutable_collection_receipt
from common.models import Activity
from common.services.operations import json_value
from customers.models import Customer,CustomerDebt,CustomerPayment,CustomerPaymentPart,DebtAllocation
from identity.models import Installation
from backups.services.recovery_collection_reviews import find_collection_review
from sales.services.pricing import MAX_FCFA


def _known_collection(capture,decoded,issues):
    operation=Activity.objects.filter(id=capture.operation_id,shop_id=capture.shop_id).first()
    if operation is None:
        if Activity.objects.filter(id=capture.operation_id).exists():
            issues.append({"code":"operation_conflict","ids":[str(capture.operation_id)]})
        return None
    receipt=operation.details.get("response")
    try:
        origin=RecoveredOperationOrigin.objects.select_related("capture").filter(operation=operation).first()
        # Current recovered allocations can be lower than the original ones.
        # Compare the preserved source, never replay an adjusted receipt as if it
        # were the original facts, nor compare against mutable payment balances.
        source=(operation.details["recovery"]["source_snapshot"] if origin else immutable_collection_receipt(receipt))
        matches=(operation.action=="customer_payment_created" and operation.target_id==decoded.snapshot["id"]
            and operation.details.get("request")==decoded.canonical_request
            and receipt["actor_id"]==str(capture.origin_user_id)
            and receipt["installation_id"]==str(capture.origin_installation_id)
            and receipt["occurred_at"]==json_value(capture.occurred_at)
            and source==json_value(decoded.snapshot)
            and CustomerPayment.objects.filter(operation=operation,id=decoded.snapshot["id"],shop_id=capture.shop_id,
                customer_id=decoded.snapshot["customer_id"],occurred_at=capture.occurred_at).exists())
        if origin:
            matches=matches and (origin.capture.operation_type=="customer_collection"
                and origin.capture.operation_id==capture.operation_id
                and origin.capture.origin_user_id==capture.origin_user_id
                and origin.capture.origin_installation_id==capture.origin_installation_id)
    except (KeyError,TypeError,ValueError):matches=False
    if not matches:issues.append({"code":"operation_conflict","ids":[str(capture.operation_id)]})
    return operation


def build_collection_preview(shop,capture):
    decoded=decode_collection_snapshot(capture)
    snapshot=decoded.snapshot
    issues=[]
    known=_known_collection(capture,decoded,issues)
    review=find_collection_review(capture,snapshot)
    if review:
        if review.state=="refused":issues.append({"code":"recovery_collection_refused","ids":[str(review.id)]})
        if review.state=="validated" and known is None:issues.append({"code":"validated_recovery_collection_missing","ids":[str(review.id)]})
        if review.state=="pending" and known is not None:issues.append({"code":"pending_recovery_collection_already_committed","ids":[str(review.id)]})
        if review.state=="pending" and review.version>=MAX_FCFA:issues.append({"code":"review_version_exhausted","ids":[str(review.id)]})
    latest=capture.integration_decisions.order_by("-version").first()
    version=latest.version if latest else 0
    customer=Customer.objects.filter(shop=shop,id=snapshot["customer_id"]).first()
    debts={row["id"]:row for row in CustomerDebt.objects.filter(shop=shop,customer_id=snapshot["customer_id"],
        id__in=[row["debt_id"] for row in snapshot["allocations"]]).values(
            "id","amount_fcfa","remaining_fcfa","operation_id","sale_id","due_at","due_version","occurred_at")}
    if known is None:
        if capture.occurred_at>timezone.now():issues.append({"code":"future_effective_date","ids":[]})
        if customer is None:issues.append({"code":"missing_customer","ids":[str(snapshot["customer_id"])]})
        for debt_id in sorted({row["debt_id"] for row in snapshot["allocations"]}-debts.keys(),key=str):
            issues.append({"code":"missing_debt","ids":[str(debt_id)]})
        ready={str(value) for value in Activity.objects.filter(shop=shop,id__in=capture.dependencies,
            details__has_key="response").values_list("id",flat=True)}
        missing=sorted(set(capture.dependencies)-ready)
        if missing:issues.append({"code":"missing_dependencies","ids":missing})
        if (CustomerPayment.objects.filter(id=snapshot["id"]).exists()
                or RecoveryCollectionReview.objects.filter(payment_id=snapshot["id"]).exclude(source_operation_id=capture.operation_id).exists()
                or CustomerPaymentPart.objects.filter(id__in=[row["id"] for row in snapshot["parts"]]).exists()
                or DebtAllocation.objects.filter(id__in=[row["id"] for row in snapshot["allocations"]]).exists()):
            issues.append({"code":"entity_uuid_conflict","ids":[]})
        source_installation=Installation.objects.filter(id=capture.origin_installation_id).values("user_id").first()
        if source_installation is not None and source_installation["user_id"]!=capture.origin_user_id:
            issues.append({"code":"origin_identity_conflict","ids":[]})
    remaining={key:row["remaining_fcfa"] for key,row in debts.items()}
    impacts=[]
    applied_total=0
    for row in snapshot["allocations"]:
        debt_id=row["debt_id"]
        if debt_id not in debts:continue
        before=remaining[debt_id]
        applied=min(before,row["applied_amount_fcfa"]) if known is None else 0
        remaining[debt_id]-=applied
        applied_total+=applied
        impacts.append({"allocation_id":row["id"],"debt_id":debt_id,
            "requested_amount_fcfa":row["requested_amount_fcfa"],"original_applied_fcfa":row["applied_amount_fcfa"],
            "remaining_before_fcfa":before,"applied_fcfa":applied,"remaining_after_fcfa":remaining[debt_id]})
    cash={}
    if known is None:
        for part in snapshot["parts"]:
            key="other:"+part["other_name"] if part["method"]=="other" else part["method"]
            cash[key]=cash.get(key,0)+part["amount_fcfa"]
    frame=json_value({"capture_id":capture.id,"source_sha256":capture.content_sha256,"version":version,
        "known_operation":known.id if known else None,"known_receipt":known.details.get("response") if known else None,
        "customer":None if customer is None else {"id":customer.id,"version":customer.version},
        "debts":sorted(debts.values(),key=lambda row:str(row["id"])),"issues":issues,
        "review":None if review is None else {"id":review.id,"state":review.state,"version":review.version}})
    token=hashlib.sha256(canonical_bytes(frame)).hexdigest()
    preview=json_value({"capture_id":capture.id,"operation_type":"customer_collection","version":version,"preview_token":token,
        "can_apply":not issues,"outcome":"blocked" if issues else "already_known" if known else "new",
        "hold_id":None,"collection_review_id":review.id if review else None,
        "review_state":review.state if review else None,"review_version":review.version if review else 0,"can_hold":False,
        "can_mark_pending":not issues and known is None and (review is None or review.state=="pending"),
        "can_refuse":review is not None and review.state=="pending" and known is None and not issues,
        "snapshot":snapshot,"stocks":[],"allocation_impacts":impacts,"cash_delta_by_method_fcfa":cash,
        "new_debt_fcfa":0,"new_customer_credit_fcfa":snapshot["amount_fcfa"]-applied_total if known is None else 0,
        "origin_user_id":capture.origin_user_id,"origin_installation_id":capture.origin_installation_id,
        "origin_recorded_at":capture.origin_recorded_at,"issues":issues})
    return preview,decoded,known,{},customer
