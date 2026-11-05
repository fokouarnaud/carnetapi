"""Current restoration decisions supplement, never replace, ordinary rights."""
from django.db.models import OuterRef, Q, Subquery
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import PermissionDenied
from backups.models import RestoreAccessDecision, RestoreAccessReview


def pending_access_reviews(user):
    latest = RestoreAccessDecision.objects.filter(review_id=OuterRef("pk")).order_by("-version")
    return RestoreAccessReview.objects.filter(user=user, generation__current_slot=1).annotate(
        last_state=Subquery(latest.values("state")[:1])).filter(
            Q(last_state__isnull=True) | Q(last_state="refused"))


def require_confirmed_access(shop, user):
    if pending_access_reviews(user).filter(shop=shop).exists():
        raise PermissionDenied(_("Access to this shop requires confirmation after restoration."), code="restore_access_unconfirmed")
