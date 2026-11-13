"""G15: real operations are backdated at most 30 days; beyond, owner only with a second reason."""
from datetime import timedelta

from django.utils import timezone
from rest_framework.exceptions import ValidationError

BACKDATE_LIMIT = timedelta(days=30)


def require_allowed_backdate(shop, actor, occurred_at, backdate_reason=None):
    if timezone.now() - occurred_at <= BACKDATE_LIMIT:
        return
    if shop.owner_id != actor.id:
        raise ValidationError({"occurred_at": "Antidatage limité à 30 jours ; au-delà, propriétaire seulement."})
    if not (backdate_reason or "").strip():
        raise ValidationError({"backdate_reason": "Second motif obligatoire au-delà de 30 jours."})
