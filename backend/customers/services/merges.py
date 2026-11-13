"""US-20 / G18: manager-only merges inside one shop, resolved by redirection, never by rewriting history."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from common.models import Activity
from customers.models import Customer
from identity.services.access import effective_permissions


def resolve_customer(customer):
    """Final target of a merge chain; an unmerged customer resolves to itself."""
    customer = Customer.objects.get(pk=customer.pk)  # never trust a stale in-memory redirection
    seen = set()
    while customer.merged_into_id is not None and customer.id not in seen:
        seen.add(customer.id)
        customer = Customer.objects.get(pk=customer.merged_into_id)
    return customer


@transaction.atomic
def merge_customers(source, target, *, actor, reason, operation_id=None):
    role, _permissions = effective_permissions(source.shop, actor)
    if role != "manager":
        raise PermissionDenied("Fusion de clients réservée au responsable.")
    if source.shop_id != target.shop_id or source.id == target.id or not (reason or "").strip():
        raise ValidationError("Fusion impossible : même boutique, fiches distinctes et motif obligatoires.")
    if resolve_customer(target).id == source.id:
        raise ValidationError("Fusion circulaire refusée.")
    source = Customer.objects.select_for_update().get(pk=source.pk)
    source.merged_into = target
    source.version += 1
    source.save(update_fields=["merged_into", "version", "updated_at"])
    identity = {"id": operation_id} if operation_id else {}
    Activity.objects.create(shop=source.shop, actor=actor, action="client_merged", target_id=source.id,
                            reason=reason.strip(), details={"merged_into": str(target.id)}, **identity)
    return source
