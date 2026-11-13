"""US-47 / G24: shop ownership is reassigned only by the technical superadministrator."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from common.models import Activity
from identity.models import Membership, OwnershipTransfer, Shop


@transaction.atomic
def transfer_ownership(shop, new_owner, *, actor, reason, proofs):
    if not actor.is_superuser:
        raise PermissionDenied("Réattribution de propriété réservée au superadministrateur technique.")
    if not (reason or "").strip() or not proofs:
        raise ValidationError("Motif et preuves obligatoires.")
    shop = Shop.objects.select_for_update().get(pk=shop.pk)
    previous = shop.owner
    shop.owner = new_owner
    shop.save(update_fields=["owner"])
    Membership.objects.filter(shop=shop, user=previous).update(is_active=False)
    record = OwnershipTransfer.objects.create(shop=shop, previous_owner=previous, new_owner=new_owner,
                                              decided_by=actor, reason=reason.strip(), proofs=proofs)
    Activity.objects.create(shop=shop, actor=actor, action="shop_ownership_transferred", target_id=record.id,
                            reason=reason.strip(), details={"previous_owner": str(previous.id), "new_owner": str(new_owner.id)})
    return record
