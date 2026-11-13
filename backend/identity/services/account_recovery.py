"""US-07 / G22: three proofs, then 72 hours before the new number replaces the lost one."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from identity.models import AccountRecoveryCase, DeviceSession, Membership, Shop

RECOVERY_DELAY = timedelta(hours=72)


def _require_superadmin(actor):
    if not actor.is_superuser:
        raise PermissionDenied("Recours exceptionnel réservé au superadministrateur technique.")


@transaction.atomic
def open_recovery_case(account, new_phone, *, actor, identity_reference, facts_checked, confirming_member, reason):
    _require_superadmin(actor)
    shops = Shop.objects.filter(Q(owner=account) | Q(memberships__user=account, memberships__is_active=True))
    confirmed = Membership.objects.filter(shop__in=shops, user=confirming_member, is_active=True).exclude(user=account).exists() \
        or Shop.objects.filter(id__in=shops, owner=confirming_member).exclude(owner=account).exists()
    if facts_checked < 3 or not confirmed or not (identity_reference or "").strip() or not (reason or "").strip():
        raise ValidationError("Trois preuves exigées : pièce vérifiée, 3 faits du compte, membre actif confirmant.")
    now = timezone.now()
    case = AccountRecoveryCase.objects.create(account=account, new_phone=new_phone, identity_reference=identity_reference.strip(),
                                              facts_checked=facts_checked, confirming_member=confirming_member,
                                              decided_by=actor, reason=reason.strip(), effective_after=now + RECOVERY_DELAY)
    # Keep both instants identical so the 72-hour wait is exact.
    AccountRecoveryCase.objects.filter(pk=case.pk).update(created_at=now)
    case.refresh_from_db()
    return case


@transaction.atomic
def complete_recovery_case(case, *, actor, now=None):
    _require_superadmin(actor)
    now = now or timezone.now()
    case = AccountRecoveryCase.objects.select_for_update().get(pk=case.pk)
    if case.completed_at is not None or now < case.effective_after:
        raise ValidationError("Délai de 72 heures non écoulé ou recours déjà clôturé.")
    account = case.account
    account.phone = case.new_phone
    account.save(update_fields=["phone"])
    DeviceSession.objects.filter(installation__user=account, revoked_at__isnull=True).update(revoked_at=now)
    case.completed_at = now
    case.save(update_fields=["completed_at", "updated_at"])
    return case
