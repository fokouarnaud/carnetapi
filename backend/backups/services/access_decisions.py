"""Record a current technical check for one restored responsible account scope."""
import uuid
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from backups.models import RestoreAccessDecision, RestoreAccessGeneration, RestoreAccessReview
from backups.services.jobs import authorize
from common.services.errors import OperationConflict
from identity.models import Membership, User


def current_review_role(review):
    """Describe the current scoped role without treating the archived role as authority."""
    if review.shop.owner_id==review.user_id:
        return "owner"
    member=review.membership
    if member is not None and member.shop_id==review.shop_id and member.user_id==review.user_id:
        return member.role
    return None


@transaction.atomic
def decide_responsible_access(*, actor, review_id, operation_id, previous_version, state, reason, evidence_ref=""):
    """Confirm current quality, without restoring devices or technical privileges.

    This service records the human check. It does not perform OTP verification
    or open the external maintenance gate.
    """
    authorize(actor)
    if (type(previous_version) is not int or not 0 <= previous_version < 9223372036854775807
            or state not in ("confirmed", "refused") or not isinstance(reason, str)
            or not reason.strip() or len(reason) > 2000 or not isinstance(evidence_ref, str)
            or len(evidence_ref) > 500):
        raise ValidationError(_("Invalid restoration access decision."), code="restore_access_decision_invalid")
    try:
        operation_id, review_id = uuid.UUID(str(operation_id)), uuid.UUID(str(review_id))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError(_("Invalid restoration access decision."), code="restore_access_decision_invalid") from None
    reason, evidence_ref = reason.strip(), evidence_ref.strip()
    # SQLite ignores SELECT FOR UPDATE. Reserve the write transaction before
    # reading review versions so two workers cannot append the same next version.
    if not RestoreAccessGeneration.objects.filter(current_slot=1).update(current_slot=1):
        raise OperationConflict()
    authorize(actor)
    review = get_object_or_404(RestoreAccessReview.objects.select_related("shop", "membership", "generation"), id=review_id)
    if review.generation.current_slot != 1:
        raise OperationConflict()
    previous = RestoreAccessDecision.objects.filter(operation_id=operation_id).first()
    if previous:
        if (previous.review_id != review.id or previous.requested_by_id != actor.id
                or previous.previous_version != previous_version or previous.state != state
                or previous.reason != reason or previous.evidence_ref != evidence_ref):
            raise OperationConflict()
        return previous, False
    membership=review.membership
    if membership is not None and (membership.shop_id!=review.shop_id or membership.user_id!=review.user_id):
        raise OperationConflict()
    role=current_review_role(review)
    if role is None and review.archived_role=="owner":
        raise OperationConflict()
    if role not in ("owner","manager"):
        raise ValidationError(_("This review requires a responsible account."),code="restore_access_responsible_required")
    latest = review.decisions.order_by("-version").first()
    if previous_version != (latest.version if latest else 0):
        raise OperationConflict()
    if state == "confirmed":
        if role == "owner":
            if review.shop.owner_id != review.user_id:
                raise OperationConflict()
        else:
            membership = review.membership
            if (membership is None or membership.shop_id != review.shop_id or membership.user_id != review.user_id
                    or membership.role != "manager"):
                raise OperationConflict()
            if not membership.is_active:
                Membership.objects.filter(id=membership.id).update(is_active=True, version=F("version")+1,updated_at=timezone.now())
        User.objects.filter(id=review.user_id, is_active=False).update(is_active=True)
    decision = RestoreAccessDecision.objects.create(review=review, operation_id=operation_id,
        requested_by=actor, previous_version=previous_version, version=previous_version+1,
        state=state, reason=reason, evidence_ref=evidence_ref)
    return decision, True
