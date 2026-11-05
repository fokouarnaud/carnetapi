from django.db.models import Exists, OuterRef, Q
from backups.services.access_guard import pending_access_reviews, require_confirmed_access

from identity.models import Membership, Shop, ShopDeviceAccess


def accessible_shops(user, installation):
    memberships = Membership.objects.filter(shop_id=OuterRef("pk"), user=user, is_active=True)
    revoked = ShopDeviceAccess.objects.filter(
        shop_id=OuterRef("pk"), installation=installation, revoked_at__isnull=False,
    )
    return (
        Shop.objects.annotate(has_membership=Exists(memberships), device_revoked=Exists(revoked),
            restoration_pending=Exists(pending_access_reviews(user).filter(shop_id=OuterRef("pk"))))
        .filter(Q(owner=user) | Q(has_membership=True), is_active=True, device_revoked=False, restoration_pending=False)
        .order_by("created_at", "id")
    )


def effective_permissions(shop, user):
    require_confirmed_access(shop, user)
    names = ["can_grant_credit", "can_collect_debt", "can_read_debt", "can_prepare_reminder", "can_read_cost", "can_read_cash_balance"]
    if shop.owner_id == user.id:
        return "manager", dict.fromkeys(names, True)
    membership = Membership.objects.get(shop=shop, user=user, is_active=True)
    if membership.role == Membership.Role.MANAGER:
        return "manager", dict.fromkeys(names, True)
    permissions = {name: getattr(membership, name) for name in names}
    permissions["can_read_debt"] = any(permissions[name] for name in names[:4])
    return "seller", permissions
