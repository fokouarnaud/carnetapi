"""Count observed recovery evidence without reading source money or claiming completeness."""
from django.db.models import (
    BigIntegerField, Case, CharField, Count, F, IntegerField, Min, OuterRef,
    Q, Subquery, Sum, Value, When,
)
from django.db.models.functions import Cast, Coalesce
from django.shortcuts import get_object_or_404

from backups.models import (
    RecoveryClosure, RecoveryCollectionReviewDecision, RecoveryDevice,
    RecoveryIntegrationDecision, RecoveryJournalOperation, RecoverySaleHold,
    RecoverySaleHoldDecision, RestoreSession,
)
from backups.services.jobs import authorize
from backups.services.recovery_journal import journal_context
from common.services.operations import json_value
from stock.models import StockReservation, StockReservationReconciliation
from stock.services.stock_queries import StockTotal

ORIGIN_FIELDS = ("shop_id", "origin_installation_id", "origin_user_id")
DISPOSITIONS = ("applied", "already_known", "pending", "refused", "without_decision")
EVIDENCE_GAPS = (
    "device_journal_completion_not_recorded",
    "journal_retention_coverage_not_recorded",
    "inventory_exhaustiveness_not_established",
)


def technical_balance_context(*, actor, restore_session_id, session=None):
    """Authorize the current technical account without granting a business role."""
    authorize(actor, session)
    return get_object_or_404(RestoreSession, id=restore_session_id, generation__current_slot=1)


def shop_balance_context(*, shop_id, actor, session_id):
    """Authorize a live, confirmed responsible before any inventory query."""
    shop, _live = journal_context(shop_id, actor, session_id)
    return get_object_or_404(RestoreSession, generation__current_slot=1), shop.id


def _captures(registry, shop_id):
    rows = RecoveryJournalOperation.objects.filter(session_id=registry.id)
    return rows.filter(shop_id=shop_id) if shop_id is not None else rows


def _devices(registry, shop_id):
    rows = RecoveryDevice.objects.filter(session_id=registry.id)
    return rows.filter(shop_id=shop_id) if shop_id is not None else rows


def balance_origins(registry, *, shop_id=None):
    """Return a SQL union for stable, bounded pagination by an authorized caller."""
    archived = _devices(registry, shop_id).order_by().values(*ORIGIN_FIELDS)
    captured = _captures(registry, shop_id).order_by().values(*ORIGIN_FIELDS)
    return archived.union(captured).order_by(*ORIGIN_FIELDS)


def _review_source(prefix):
    return {prefix + "source_operation_id": OuterRef("operation_id"),
            prefix + "capture__shop_id": OuterRef("shop_id"),
            prefix + "capture__content_sha256": OuterRef("content_sha256")}


def _classified_captures(registry, shop_id):
    # Every subquery selects metadata only. A business UUID alone is not a receipt.
    integration = RecoveryIntegrationDecision.objects.filter(capture_id=OuterRef("pk")).order_by("-version", "-id")
    sale = RecoverySaleHoldDecision.objects.filter(**_review_source("hold__"),
        capture__shop_id=OuterRef("shop_id"), capture__content_sha256=OuterRef("content_sha256"))
    collection = RecoveryCollectionReviewDecision.objects.filter(**_review_source("review__"),
        capture__shop_id=OuterRef("shop_id"), capture__content_sha256=OuterRef("content_sha256"))
    hold = RecoverySaleHold.objects.filter(**_review_source("")).order_by()
    rows = _captures(registry, shop_id).annotate(
        balance_integration=Subquery(integration.values("outcome")[:1]),
        balance_integration_source=Subquery(integration.values("source_sha256")[:1]),
        balance_integration_shop=Subquery(integration.values("business_operation__shop_id")[:1]),
        balance_sale_review=Subquery(sale.order_by("-version", "-id").values("state")[:1]),
        balance_collection_review=Subquery(collection.order_by("-version", "-id").values("state")[:1]),
        balance_hold_operation=Subquery(hold.values("reservation_operation_id")[:1]),
    )
    verified = Q(balance_integration_source=F("content_sha256"), balance_integration_shop=F("shop_id"))
    rows = rows.annotate(balance_disposition=Case(
        When(verified & Q(balance_integration="applied"), then=Value("applied")),
        When(verified & Q(balance_integration="already_known"), then=Value("already_known")),
        When(operation_type="sale", balance_sale_review="pending", then=Value("pending")),
        When(operation_type="sale", balance_sale_review="refused", then=Value("refused")),
        When(operation_type="customer_collection", balance_collection_review="pending", then=Value("pending")),
        When(operation_type="customer_collection", balance_collection_review="refused", then=Value("refused")),
        default=Value("without_decision"), output_field=CharField(),
    ))
    released = StockReservationReconciliation.objects.filter(reservation_id=OuterRef("pk")).order_by().values(
        "reservation_id").annotate(total=StockTotal("quantity_milli")).values("total")
    remaining = StockReservation.objects.filter(operation_id=OuterRef("balance_hold_operation"),
        product__shop_id=OuterRef("shop_id"), state="rejected").annotate(
            balance_released=Cast(Coalesce(Subquery(released), Value("0")), BigIntegerField()))
    remaining = remaining.filter(quantity_milli__gt=F("balance_released")).order_by().values(
        "operation_id").annotate(number=Count("id")).values("number")
    return rows.annotate(balance_remaining_reservations=Case(
        When(balance_disposition="refused", operation_type="sale",
             then=Coalesce(Subquery(remaining, output_field=IntegerField()), Value(0))),
        default=Value(0), output_field=IntegerField(),
    ))


def _counts():
    return {"captured": Count("id", distinct=True), **{
        state: Count("id", distinct=True, filter=Q(balance_disposition=state)) for state in DISPOSITIONS},
        "refused_reservations_remaining": Coalesce(Sum("balance_remaining_reservations"), Value(0))}


def _key(row, *, pair=False):
    return tuple(str(row[name]) for name in ORIGIN_FIELDS[:2 if pair else 3])


def _page_filter(origins, *, pair=False):
    condition = Q(pk__in=[])
    fields = ORIGIN_FIELDS[:2 if pair else 3]
    for key in {_key(row, pair=pair) for row in origins}:
        condition |= Q(**dict(zip(fields, key)))
    return condition


def build_balance(registry, origins, *, shop_id=None):
    """Enrich only the caller's bounded origin page; totals cover its whole scope.

    Dispositions describe the latest recorded decision for this capture, not a
    new validation. Pending/refused reviews can refer to the same exact source
    in an older generation. Completion and retention evidence do not yet exist.
    """
    captures = _classified_captures(registry, shop_id)
    totals = captures.aggregate(**_counts())
    remaining = totals.pop("refused_reservations_remaining")
    archived_count = _devices(registry, shop_id).count()
    origin_count = balance_origins(registry, shop_id=shop_id).count()
    groups, archive, identities = {}, {}, {}
    if origins:
        groups = {_key(row): row for row in captures.filter(_page_filter(origins)).order_by().values(
            *ORIGIN_FIELDS).annotate(**_counts())}
        archive = {_key(row, pair=True): str(row["origin_user_id"]) for row in _devices(registry, shop_id).filter(
            _page_filter(origins, pair=True)).values(*ORIGIN_FIELDS)}
        identities = {_key(row, pair=True): row for row in _captures(registry, shop_id).filter(
            _page_filter(origins, pair=True)).order_by().values(*ORIGIN_FIELDS[:2]).annotate(
                users=Count("origin_user_id", distinct=True), first_user=Min("origin_user_id"))}
    results = []
    for origin in origins:
        group = groups.get(_key(origin), {})
        archived_user = archive.get(_key(origin, pair=True))
        identity = identities.get(_key(origin, pair=True), {})
        first_user = str(identity.get("first_user")) if identity else None
        conflict = (identity.get("users", 0) > 1 or (archived_user is not None and
            ((first_user is not None and first_user != archived_user) or str(origin["origin_user_id"]) != archived_user)))
        results.append({**{name: origin[name] for name in ORIGIN_FIELDS},
            "seen_in_archived_inventory": archived_user == str(origin["origin_user_id"]),
            "origin_identity_conflict": conflict,
            "captures": {name: group.get(name, 0) for name in ("captured", *DISPOSITIONS)},
            "refused_reservations_remaining": group.get("refused_reservations_remaining", 0),
            "journal_complete": None, "coverage_since_snapshot": None})
    summary = {"session_id": registry.id, "generation_id": registry.generation_id, "shop_id": shop_id,
        "snapshot_at": registry.snapshot_at, "inventory_recorded_at": registry.inventory_recorded_at,
        "archived_device_observations": archived_count if registry.inventory_recorded_at is not None else None,
        "origin_count": origin_count, "origins_outside_archived_inventory": origin_count - archived_count,
        "captures": totals, "refused_reservations_remaining": remaining,
        "classification_basis": "latest_recorded_decisions", "inventory_exhaustive": None,
        "evidence_gaps": list(EVIDENCE_GAPS), "can_close_complete": False,
        "exhaustive_recovery_guaranteed": False,
        "closure_id": RecoveryClosure.objects.filter(session_id=registry.id).values_list("id", flat=True).first()}
    return json_value({"summary": summary, "results": results})
