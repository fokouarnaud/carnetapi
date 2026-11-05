"""Inspect current restoration reviews after technical authorization."""
from django.db.models import OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from rest_framework.exceptions import ValidationError
from backups.models import RestoreAccessDecision, RestoreAccessGeneration, RestoreAccessReview
from backups.services.jobs import authorize


def inspect_access_reviews(actor, *, page=1, page_size=25, state="all", shop_id=None):
    authorize(actor)
    if (type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 100
            or state not in ("all", "pending", "confirmed", "refused")):
        raise ValidationError(code="restore_access_inspection_invalid")
    generation = RestoreAccessGeneration.objects.get(current_slot=1)
    latest = RestoreAccessDecision.objects.filter(review_id=OuterRef("pk")).order_by("-version")
    reviews = RestoreAccessReview.objects.filter(generation=generation).annotate(
        current_state=Coalesce(Subquery(latest.values("state")[:1]), Value("pending")),
        current_version=Coalesce(Subquery(latest.values("version")[:1]), Value(0)))
    if shop_id is not None:
        reviews = reviews.filter(shop_id=shop_id)
    if state != "all":
        reviews = reviews.filter(current_state=state)
    count = reviews.count()
    offset = (page-1)*page_size
    if page > 1 and offset >= count:
        raise ValidationError(code="restore_access_inspection_invalid")
    rows = reviews.select_related("shop", "user").order_by("created_at", "id")[offset:offset+page_size]
    return {"generation_id":str(generation.id), "count":count, "page":page, "page_size":page_size,
        "results":[{"id":str(row.id), "shop_id":str(row.shop_id), "shop_name":row.shop.name,
            "user_id":str(row.user_id), "archived_username":row.user.username,
            "archived_role":row.archived_role, "was_active":row.was_active,
            "state":row.current_state, "version":row.current_version} for row in rows]}
