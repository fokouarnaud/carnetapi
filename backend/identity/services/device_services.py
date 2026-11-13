from datetime import timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import PermissionDenied

from common.services.errors import OperationConflict
from identity.services.access import effective_permissions
from identity.services.authentication import require_live_session
from common.services.operations import current_shop
from backups.services.access_guard import require_confirmed_access
from common.models import Activity
from identity.models import Installation, Membership, ShopDeviceAccess
from identity.services.installation_codes import assign_installation_code


@transaction.atomic
def verify_access(shop, installation, user, session_id):
    require_live_session(user, session_id, installation)
    shop = current_shop(shop.id, user, installation)
    role, permissions = effective_permissions(shop, user)
    now = timezone.now()
    grant, _ = ShopDeviceAccess.objects.get_or_create(shop=shop, installation=installation)
    # Re-check inside the write transaction; verification never cancels a revocation.
    if grant.revoked_at is not None:
        raise PermissionDenied("Installation retirée de cette boutique.")
    grant.verified_at = now
    grant.offline_expires_at = now + timedelta(days=7)
    grant.save(update_fields=["verified_at", "offline_expires_at", "updated_at"])
    return {
        "shop_id": shop.id, "installation_id": installation.id,
        "verified_at": now, "offline_expires_at": grant.offline_expires_at,
        "role": role, "permissions": permissions,
        "seller_discount_cap_basis_points": shop.seller_discount_cap_basis_points,
        "discount_policy_version": shop.discount_policy_version,
        "installation_code": assign_installation_code(shop, installation).label,
    }


@transaction.atomic
def revoke_device(shop, actor, installation_id, reason, operation_id, *, session_id, actor_installation):
    require_confirmed_access(shop, actor)
    session = require_live_session(actor, session_id, actor_installation)
    shop = current_shop(shop.id, actor, session.installation)
    is_owner = shop.owner_id == actor.id
    if not is_owner and not Membership.objects.filter(
        shop=shop, user=actor, is_active=True, role=Membership.Role.MANAGER,
    ).exists():
        raise PermissionDenied("Retrait réservé au responsable.")

    target = get_object_or_404(Installation, id=installation_id)
    target_membership = Membership.objects.filter(shop=shop, user_id=target.user_id).first()
    target_is_owner = shop.owner_id == target.user_id
    if not target_is_owner and target_membership is None:
        from rest_framework.exceptions import NotFound
        raise NotFound()
    if not is_owner and (target_is_owner or target_membership.role == Membership.Role.MANAGER):
        raise PermissionDenied("Seul le propriétaire peut retirer l'appareil d'un responsable.")

    existing = Activity.objects.filter(id=operation_id).first()
    if existing is not None:
        if (
            existing.shop_id != shop.id or existing.actor_id != actor.id
            or existing.action != "device_revoked" or existing.target_id != target.id
            or existing.reason != reason
        ):
            raise OperationConflict()
        # Return the original receipt even if later administration changes the grant.
        return {"operation_id": operation_id, "installation_id": target.id, "revoked_at": parse_datetime(existing.details["revoked_at"])}

    from backups.models import RecoveryClosure, RecoveryIntegrationDecision
    if (RecoveryClosure.objects.filter(operation_id=operation_id).exists()
            or RecoveryIntegrationDecision.objects.filter(operation_id=operation_id).exists()):
        raise OperationConflict()
    grant, _ = ShopDeviceAccess.objects.get_or_create(shop=shop, installation=target)
    now = timezone.now()
    if grant.revoked_at is None:
        grant.revoked_at = now
        grant.save(update_fields=["revoked_at", "updated_at"])
    Activity.objects.create(
        id=operation_id, shop=shop, actor=actor, action="device_revoked",
        target_id=target.id, reason=reason,
        details={"installation_id": str(target.id), "revoked_at": grant.revoked_at.isoformat()},
    )
    return {"operation_id": operation_id, "installation_id": target.id, "revoked_at": grant.revoked_at}
