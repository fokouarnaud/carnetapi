"""A current responsible explicitly revalidates one seller and a fresh permission profile."""
import uuid
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError,PermissionDenied
from backups.access_models import PERMISSION_FIELDS
from backups.models import RestoreAccessGeneration,RestoreAccessReview,RestoreAccessDecision
from common.models import Activity
from common.services.errors import OperationConflict
from common.services.operations import current_shop,require_manager,original_receipt,json_value
from identity.models import Membership,User
from identity.services.authentication import require_live_session


ACTION="restore_seller_access_decided"


class RestoreSellerAccessConflict(OperationConflict):
    default_detail=_("The restoration context changed or this operation UUID belongs to another request.")


def _invalid():
    raise ValidationError(_("Invalid seller access decision."),code="restore_seller_decision_invalid")


def _validated(data):
    required={"operation_id","generation_id","previous_review_version","membership_version","state","reason"}
    if not isinstance(data,dict) or not required<=set(data) or set(data)-required-{"evidence_ref","permissions"}:
        _invalid()
    value=dict(data)
    for name,minimum in (("previous_review_version",0),("membership_version",1)):
        if type(value[name]) is not int or not minimum<=value[name]<9223372036854775807:
            _invalid()
    if value["state"] not in ("confirmed","refused"):
        _invalid()
    for name,maximum in (("reason",2000),("evidence_ref",500)):
        item=value.get(name,"")
        if not isinstance(item,str) or len(item)>maximum or (name=="reason" and not item.strip()):
            _invalid()
        value[name]=item.strip()
    try:
        for name in ("operation_id","generation_id"):
            value[name]=uuid.UUID(str(value[name]))
    except (ValueError,TypeError,AttributeError):
        _invalid()
    if value["state"]=="confirmed":
        permissions=value.get("permissions")
        if (not isinstance(permissions,dict) or set(permissions)!=set(PERMISSION_FIELDS)
                or any(type(item) is not bool for item in permissions.values())):
            _invalid()
        value["permissions"]=dict(permissions)
    elif "permissions" in value:
        _invalid()
    return value


def _profile(member,state):
    declared={name:getattr(member,name) for name in PERMISSION_FIELDS}
    effective=dict.fromkeys(PERMISSION_FIELDS,False)
    if member.is_active and member.user.is_active and state=="confirmed":
        if member.role=="manager":
            effective=dict.fromkeys(PERMISSION_FIELDS,True)
        else:
            effective=dict(declared)
            effective["can_read_debt"]=any(declared[name] for name in (
                "can_read_debt","can_grant_credit","can_collect_debt","can_prepare_reminder"))
    return json_value({"id":member.id,"user_id":member.user_id,"role":member.role,
        "is_active":member.is_active,"version":member.version,"account_active":member.user.is_active,
        "declared_permissions":declared,"permissions":effective})


def _scope(shop_id,member_id,actor,installation,session_id):
    require_live_session(actor,session_id,installation)
    shop=current_shop(shop_id,actor,installation)
    require_manager(shop,actor)
    member=get_object_or_404(Membership.objects.select_related("user"),id=member_id,shop=shop)
    return shop,member


@transaction.atomic
def inspect_seller_access(*,shop_id,member_id,actor,installation,session_id):
    shop,member=_scope(shop_id,member_id,actor,installation,session_id)
    review=get_object_or_404(RestoreAccessReview,generation__current_slot=1,shop=shop,user_id=member.user_id,membership=member)
    latest=review.decisions.order_by("-version").first()
    state=latest.state if latest else "pending"
    return json_value({"review_id":review.id,"generation_id":review.generation_id,"archived_role":review.archived_role,
        "archived_permissions":review.archived_permissions,"state":state,"version":latest.version if latest else 0,
        "membership":_profile(member,state)})


@transaction.atomic
def revalidate_seller_access(*,shop_id,member_id,actor,installation,session_id,data):
    shop,member=_scope(shop_id,member_id,actor,installation,session_id)
    value=_validated(data)
    # Reserve SQLite's writer before version reads; no restored flag is granted
    # merely because it appears in the archived membership or its snapshot.
    if not RestoreAccessGeneration.objects.filter(current_slot=1,id=value["generation_id"]).update(current_slot=1):
        raise RestoreSellerAccessConflict()
    review=get_object_or_404(RestoreAccessReview.objects.select_related("generation"),
        generation_id=value["generation_id"],shop=shop,user_id=member.user_id,membership=member)
    canonical=json_value({**value,"membership_id":member.id})
    try:
        receipt=original_receipt(value["operation_id"],shop,actor,ACTION,canonical)
    except OperationConflict:
        raise RestoreSellerAccessConflict() from None
    if receipt is not None:
        return receipt,False
    if RestoreAccessDecision.objects.filter(operation_id=value["operation_id"]).exists():
        raise RestoreSellerAccessConflict()
    if member.role!="seller" or shop.owner_id==member.user_id:
        raise ValidationError(_("This control requires a current seller."),code="restore_seller_required")
    latest=review.decisions.order_by("-version").first()
    version=latest.version if latest else 0
    if value["previous_review_version"]!=version or value["membership_version"]!=member.version:
        raise RestoreSellerAccessConflict()
    before=_profile(member,latest.state if latest else "pending")
    if value["state"]=="confirmed":
        if not member.user.is_active:
            already_released=RestoreAccessDecision.objects.filter(review__generation=review.generation,
                review__user_id=member.user_id,state="confirmed").exists()
            if member.user.is_staff or member.user.is_superuser or already_released:
                raise PermissionDenied(_("This account requires a separate current account check."),code="restore_account_check_required")
            User.objects.filter(id=member.user_id,is_active=False).update(is_active=True)
            member.user.refresh_from_db(fields=["is_active"])
        for name,item in value["permissions"].items():
            setattr(member,name,item)
        member.is_active=True
    else:
        member.is_active=False
    member.version+=1
    member.save(update_fields=[*PERMISSION_FIELDS,"is_active","version","updated_at"])
    decision=RestoreAccessDecision.objects.create(review=review,operation_id=value["operation_id"],requested_by=actor,
        previous_version=version,version=version+1,state=value["state"],reason=value["reason"],evidence_ref=value["evidence_ref"])
    after=_profile(member,value["state"])
    receipt=json_value({"review_id":review.id,"generation_id":review.generation_id,"decision_id":decision.id,
        "operation_id":value["operation_id"],"state":decision.state,"version":decision.version,"membership":after})
    Activity.objects.create(id=value["operation_id"],shop=shop,actor=actor,action=ACTION,target_id=member.id,reason=value["reason"],
        details={"request":canonical,"before":before,"after":after,"response":receipt,
            "installation_id":str(installation.id),"session_id":str(session_id),"review_id":str(review.id)})
    return receipt,True
