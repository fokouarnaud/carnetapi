"""Record current reviews without treating pending/refused money as cash."""
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from backups.models import RecoveryCollectionReview,RecoveryCollectionReviewDecision,RecoveryIntegrationDecision,RecoveryJournalOperation,RestoreAccessGeneration,RecoveryClosure
from backups.services.recovery_integration import RecoveryIntegrationInput,RecoveryTypeUnsupported
from common.models import Activity
from common.services.errors import OperationConflict
from common.services.operations import json_value
from sales.services.pricing import MAX_FCFA


class RecoveryCollectionReviewInput(RecoveryIntegrationInput):
    state=serializers.ChoiceField(choices=["pending","refused"])


class RecoveryCollectionReviewDenied(PermissionDenied):
    default_detail=_("Only a current shop responsible can review recovered collections.")


def collection_review_context(shop_id,actor,session_id):
    from backups.services.recovery_integration import integration_context,RecoveryIntegrationDenied
    try:return integration_context(shop_id,actor,session_id)
    except RecoveryIntegrationDenied:raise RecoveryCollectionReviewDenied() from None


def find_collection_review(capture,snapshot=None):
    review=RecoveryCollectionReview.objects.filter(source_operation_id=capture.operation_id,
        capture__shop_id=capture.shop_id).select_related("capture").defer("capture__payload","capture__dependencies").first()
    if review is None:
        if RecoveryCollectionReview.objects.filter(source_operation_id=capture.operation_id).exists():raise OperationConflict()
        return None
    if (review.capture.content_sha256!=capture.content_sha256 or review.capture.operation_id!=review.source_operation_id
            or review.capture.operation_type!="customer_collection"
            or review.capture.origin_user_id!=capture.origin_user_id
            or review.capture.origin_installation_id!=capture.origin_installation_id
            or review.capture.occurred_at!=capture.occurred_at):raise OperationConflict()
    latest=review.decisions.order_by("-version").values("version","state").first()
    if latest!={"version":review.version,"state":review.state}:raise OperationConflict()
    if snapshot is not None and (review.payment_id!=snapshot["id"] or review.customer_id!=snapshot["customer_id"]
            or review.amount_fcfa!=snapshot["amount_fcfa"] or review.original_unallocated_fcfa!=snapshot["unallocated_fcfa"]
            or review.original_applied_fcfa!=sum(row["applied_amount_fcfa"] for row in snapshot["allocations"])):
        raise OperationConflict()
    return review


def _record(*,review,capture,actor,installation,request,state,integration=None):
    if review.version>=MAX_FCFA:raise OperationConflict()
    event=Activity.objects.create(id=request["operation_id"],shop_id=capture.shop_id,actor=actor,
        action="recovery_collection_"+state,target_id=review.payment_id,reason=request["reason"],
        details={"request":request,"source":json_value({"capture_id":capture.id,"operation_id":capture.operation_id,
            "origin_user_id":capture.origin_user_id,"origin_installation_id":capture.origin_installation_id,
            "source_sha256":capture.content_sha256})})
    decision=RecoveryCollectionReviewDecision.objects.create(review=review,capture=capture,operation=event,
        reviewed_by=actor,reviewed_installation=installation,previous_version=review.version,version=review.version+1,
        state=state,reason=request["reason"],request=request,receipt={},integration_decision=integration)
    review.state=state;review.version=decision.version
    review.save(update_fields=["state","version","updated_at"])
    receipt=json_value({"id":decision.id,"review_id":review.id,"capture_id":capture.id,"operation_id":event.id,
        "source_operation_id":review.source_operation_id,"source_payment_id":review.payment_id,"customer_id":review.customer_id,
        "state":state,"version":decision.version,"reason":decision.reason,"reviewed_by":actor.id,
        "reviewed_installation_id":installation.id,"reviewed_at":decision.created_at,
        "integration_decision_id":integration.id if integration else None,"amount_fcfa":review.amount_fcfa,
        "original_applied_fcfa":review.original_applied_fcfa,"original_unallocated_fcfa":review.original_unallocated_fcfa})
    decision.receipt=receipt;decision.save(update_fields=["receipt","updated_at"])
    event.details["response"]=receipt;event.save(update_fields=["details","updated_at"])
    return decision


def complete_collection_review(*,review,capture,actor,installation,request,integration):
    if review is None or review.state!="pending":return None
    reviewed={**request,"state":"validated","integration_previous_version":request["previous_version"],"previous_version":review.version}
    return _record(review=review,capture=capture,actor=actor,installation=installation,request=reviewed,state="validated",integration=integration)


@transaction.atomic
def decide_collection_review(*,shop_id,actor,session_id,capture_id,data):
    from backups.services.recovery_integration import _capture,_build_preview
    shop,live=collection_review_context(shop_id,actor,session_id)
    if not RestoreAccessGeneration.objects.filter(current_slot=1).update(current_slot=1):raise OperationConflict()
    shop,live=collection_review_context(shop_id,actor,session_id)
    capture=_capture(shop,capture_id)
    if capture.operation_type!="customer_collection":raise RecoveryTypeUnsupported()
    incoming=RecoveryCollectionReviewInput(data=data);incoming.is_valid(raise_exception=True)
    request=json_value(incoming.validated_data)
    previous=RecoveryCollectionReviewDecision.objects.filter(operation_id=request["operation_id"]).first()
    if previous is not None:
        if (previous.capture_id!=capture.id or previous.reviewed_by_id!=actor.id
                or previous.reviewed_installation_id!=live.installation_id or previous.request!=request):raise OperationConflict()
        return previous.receipt,False
    if (Activity.objects.filter(id=request["operation_id"]).exists()
            or RecoveryClosure.objects.filter(operation_id=request["operation_id"]).exists()
            or RecoveryJournalOperation.objects.filter(operation_id=request["operation_id"]).exists()
            or RecoveryIntegrationDecision.objects.filter(operation_id=request["operation_id"]).exists()):raise OperationConflict()
    preview,decoded,_known,_quantities,_customer=_build_preview(shop,capture)
    allowed=preview["can_mark_pending"] if request["state"]=="pending" else preview["can_refuse"]
    if (not allowed or request["preview_token"]!=preview["preview_token"]
            or request["previous_version"]!=preview["review_version"]):raise OperationConflict()
    review=find_collection_review(capture,decoded.snapshot)
    if review is None:
        review=RecoveryCollectionReview.objects.create(capture=capture,source_operation_id=capture.operation_id,
            payment_id=decoded.snapshot["id"],customer_id=decoded.snapshot["customer_id"],amount_fcfa=decoded.snapshot["amount_fcfa"],
            original_applied_fcfa=sum(row["applied_amount_fcfa"] for row in decoded.snapshot["allocations"]),
            original_unallocated_fcfa=decoded.snapshot["unallocated_fcfa"])
    decision=_record(review=review,capture=capture,actor=actor,installation=live.installation,request=request,state=request["state"])
    return decision.receipt,True
