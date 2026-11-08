"""Keep pack-price proposals apart until a current manager chooses explicitly."""
from django.core import signing
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from catalogue.models import Product, ProductPack, PackPriceConflict, PackPriceProposal, PackPriceResolution
from catalogue.services.price_conflicts import MAX_INTEGER, _command, _integer, _uuid, _frame_digest
from catalogue.services.price_operation_attestations import catalogue_state, conflict_state, operation_snapshot
from common.models import Activity
from common.services.errors import OperationConflict
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from identity.services.authentication import require_live_session

RECORD_ACTION = "catalogue_pack_price_conflict_recorded"
RESOLVE_ACTION = "catalogue_pack_price_resolved"
TOKEN_SALT = "catalogue.pack-price-conflict.v1"
PROPOSALS_PAGE_SIZE = 25


def _context(shop_id, product_id, pack_id, actor, installation, session_id):
    require_live_session(actor, session_id, installation)
    shop = current_shop(shop_id, actor, installation)
    require_manager(shop, actor)
    product = get_object_or_404(Product, shop=shop, id=product_id)
    return shop, product, get_object_or_404(ProductPack, product=product, id=pack_id)


def ensure_pack_price_uncontested(pack_id):
    """Keep price and factor changes behind an unresolved pack-price decision."""
    if PackPriceConflict.objects.filter(pack_id=pack_id, state="open").exists():
        raise OperationConflict()


def _proposal_state(proposal):
    return {"id": proposal.id, "operation_id": proposal.operation_id, "actor_id": proposal.actor_id,
        "installation_id": proposal.installation_id, "proposed_price_fcfa": proposal.proposed_price_fcfa,
        "reason": proposal.reason, "created_at": proposal.created_at}


def pack_price_conflict_state(conflict, pack=None, product=None):
    """Expose a bounded first page and a reference to every preserved proposal."""
    pack = pack or conflict.pack
    product = product or pack.product
    if (conflict.pack_id != pack.id or conflict.product_id != product.id
            or pack.product_id != product.id or product.shop_id != conflict.shop_id):
        raise OperationConflict()
    try:
        decision = conflict.resolution
    except PackPriceResolution.DoesNotExist:
        decision = None
    count = (conflict.receipt_proposals_count if hasattr(conflict, "receipt_proposals_count")
        else conflict.proposals.count())
    proposals = (conflict.receipt_proposals if hasattr(conflict, "receipt_proposals")
        else conflict.proposals.order_by("created_at", "id")[:PROPOSALS_PAGE_SIZE])
    return json_value({"id": conflict.id, "product_id": product.id, "pack_id": pack.id,
        "source_change_id": conflict.source_change_id, "base_version": conflict.base_version,
        "base_quantity_milli": conflict.base_quantity_milli, "common_price_fcfa": conflict.common_price_fcfa,
        "state": conflict.state, "version": conflict.version, "pack_version": pack.version,
        "product_version": product.version, "created_at": conflict.created_at,
        "proposals_count": count,
        "proposals_url": f"/api/v1/shops/{conflict.shop_id}/products/{product.id}/packs/{pack.id}/price-conflicts/{conflict.id}/proposals/",
        "proposals": [_proposal_state(row) for row in proposals],
        "resolution": None if decision is None else {"operation_id": decision.operation_id,
            "proposal_id": decision.proposal_id, "price_fcfa": decision.price_fcfa,
            "actor_id": decision.actor_id, "reason": decision.reason, "created_at": decision.created_at}})


def _source_proof(source, product, pack, base_version):
    details = source.details
    if not isinstance(details, dict):
        raise OperationConflict()
    request, before, after = (details.get(key) for key in ("request", "before", "after"))
    if not all(isinstance(value, dict) for value in (request, before, after)):
        raise OperationConflict()
    if (source.action != "pack_updated" or source.target_id != pack.id or source.shop_id != product.shop_id
            or not isinstance(source.reason, str) or not source.reason.strip() or details.get("response") != after
            or request.get("reason") != source.reason
            or set(request) - {"operation_id", "product_id", "pack_id", "base_version", "reason",
                "name", "is_active", "base_quantity_milli", "sale_price_fcfa"}
            or request.get("operation_id") != str(source.id) or request.get("product_id") != str(product.id)
            or request.get("pack_id") != str(pack.id) or type(request.get("base_version")) is not int
            or request["base_version"] != base_version or type(before.get("version")) is not int
            or before["version"] != base_version or type(after.get("version")) is not int
            or after["version"] != base_version + 1):
        raise OperationConflict()
    for state in (before, after):
        if (state.get("id") != str(pack.id) or state.get("product_id") != str(product.id)
                or type(state.get("sale_price_fcfa")) is not int or not 0 <= state["sale_price_fcfa"] <= MAX_INTEGER
                or type(state.get("base_quantity_milli")) is not int or not 1 <= state["base_quantity_milli"] <= MAX_INTEGER
                or type(state.get("product_version")) is not int or not 1 <= state["product_version"] <= MAX_INTEGER
                or not isinstance(state.get("name"), str) or not state["name"].strip() or len(state["name"]) > 80
                or type(state.get("is_active")) is not bool
                or not all(isinstance(state.get(key), str) for key in ("created_at", "updated_at", "product_updated_at"))):
            raise OperationConflict()
    for key, kind in (("name", str), ("is_active", bool), ("base_quantity_milli", int)):
        if ((key in request and type(request[key]) is not kind)
                or after[key] != request.get(key, before[key])):
            raise OperationConflict()
    if (type(request.get("sale_price_fcfa")) is not int
            or request["sale_price_fcfa"] != after["sale_price_fcfa"]
            or before["sale_price_fcfa"] == after["sale_price_fcfa"]
            or after["product_version"] != before["product_version"] + 1
            or any(before.get(key) != after.get(key) for key in
                ("base_quantity_milli", "created_at"))):
        raise OperationConflict()
    actor_id, installation_id = source.actor_id, None
    try:
        capture = source.recovered_origin.capture
    except ObjectDoesNotExist:
        capture = None
    if capture is not None:
        if (capture.shop_id != source.shop_id or capture.operation_id != source.id
                or capture.operation_type != "pack_update"):
            raise OperationConflict()
        actor_id, installation_id = capture.origin_user_id, capture.origin_installation_id
    return json_value({"request": request, "before": before, "after": after,
        "actor_id": actor_id, "installation_id": installation_id, "reason": source.reason})


def _advance_pack(product, pack, price):
    if product.version >= MAX_INTEGER or pack.version >= MAX_INTEGER:
        raise OperationConflict()
    pack.sale_price_fcfa = price
    pack.version += 1
    pack.save(update_fields=["sale_price_fcfa", "version", "updated_at"])
    product.version += 1
    product.save(update_fields=["version", "updated_at"])


@transaction.atomic
def record_pack_price_conflict(shop_id, product_id, pack_id, actor, installation, validated_data, session_id):
    shop, product, pack = _context(shop_id, product_id, pack_id, actor, installation, session_id)
    data = _command(validated_data, ("operation_id", "source_change_id", "base_version", "proposed_price_fcfa", "reason"))
    data["source_change_id"] = _uuid(data["source_change_id"])
    _integer(data["base_version"], 1)
    _integer(data["proposed_price_fcfa"])
    canonical = json_value({**data, "product_id": product.id, "pack_id": pack.id})
    original = original_receipt(data["operation_id"], shop, actor, RECORD_ACTION, canonical)
    if original is not None:
        return original, False
    source = get_object_or_404(Activity, shop=shop, id=data["source_change_id"])
    proof = _source_proof(source, product, pack, data["base_version"])
    conflict = PackPriceConflict.objects.filter(pack=pack, state="open").first()
    if conflict is not None:
        if (conflict.shop_id != shop.id or conflict.product_id != product.id
                or conflict.source_change_id != source.id or conflict.source_snapshot != proof
                or conflict.base_version != data["base_version"] or conflict.base_quantity_milli != pack.base_quantity_milli
                or pack.sale_price_fcfa != conflict.common_price_fcfa or pack.version < conflict.base_version + 2
                or conflict.version >= MAX_INTEGER):
            raise OperationConflict()
    elif (pack.version != proof["after"]["version"] or product.version != proof["after"]["product_version"]
            or pack.sale_price_fcfa != proof["after"]["sale_price_fcfa"]
            or pack.base_quantity_milli != proof["after"]["base_quantity_milli"]
            or PackPriceConflict.objects.filter(source_change=source).exists()):
        raise OperationConflict()
    before, conflict_before = catalogue_state(product, pack), conflict_state(conflict)
    proposals_created = []
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor, action=RECORD_ACTION,
        target_id=pack.id, reason=data["reason"], details={"request": canonical,
            "before": {"version": pack.version, "product_version": product.version, "sale_price_fcfa": pack.sale_price_fcfa}})
    if conflict is None:
        conflict = PackPriceConflict.objects.create(shop=shop, product=product, pack=pack, source_change=source,
            opening_operation=event, base_version=data["base_version"], base_quantity_milli=pack.base_quantity_milli,
            common_price_fcfa=proof["before"]["sale_price_fcfa"], source_snapshot=proof)
        proposals_created.append(PackPriceProposal.objects.create(conflict=conflict, operation=source, actor_id=proof["actor_id"],
            installation_id=proof["installation_id"], is_native=True,
            proposed_price_fcfa=proof["after"]["sale_price_fcfa"], reason=source.reason))
        _advance_pack(product, pack, conflict.common_price_fcfa)
    else:
        conflict.version += 1
        conflict.save(update_fields=["version", "updated_at"])
    proposal = PackPriceProposal.objects.create(conflict=conflict, operation=event, actor_id=actor.id,
        installation_id=installation.id if installation else None, proposed_price_fcfa=data["proposed_price_fcfa"], reason=data["reason"])
    receipt = pack_price_conflict_state(conflict, pack, product)
    # The caller must retain this operation's identity even when it falls
    # outside the bounded first page. Frozen older receipts return above.
    receipt["recorded_proposal"] = json_value(_proposal_state(proposal))
    proposals_created.append(proposal)
    snapshot = operation_snapshot(event=event, installation=installation, product=product, pack=pack,
        conflict=conflict, before=before, conflict_before=conflict_before, proposals=proposals_created)
    receipt["operation_snapshot"] = snapshot
    event.details["operation_snapshot"] = snapshot
    event.details.update(after={"version": pack.version, "product_version": product.version, "sale_price_fcfa": pack.sale_price_fcfa}, response=receipt)
    event.save(update_fields=["details", "updated_at"])
    return receipt, True


def _preview_frame(conflict, product, pack, actor, installation, session_id):
    # Immutable proposals can change only through a new, audited revision. The
    # token therefore binds all choices without loading their entire history.
    return {"conflict_id": str(conflict.id), "conflict_version": conflict.version, "state": conflict.state,
        "shop_id": str(conflict.shop_id), "product_id": str(product.id), "pack_id": str(pack.id),
        "product_version": product.version, "pack_version": pack.version,
        "common_price_fcfa": conflict.common_price_fcfa, "price_fcfa": pack.sale_price_fcfa,
        "factor_milli": pack.base_quantity_milli, "actor_id": str(actor.id),
        "installation_id": str(installation.id) if installation else None, "session_id": str(session_id)}


@transaction.atomic
def preview_pack_price_conflict(shop_id, product_id, pack_id, conflict_id, actor, installation, session_id):
    shop, product, pack = _context(shop_id, product_id, pack_id, actor, installation, session_id)
    conflict = get_object_or_404(PackPriceConflict, shop=shop, product=product, pack=pack, id=conflict_id)
    frame = _preview_frame(conflict, product, pack, actor, installation, session_id)
    can_resolve = conflict.state == "open" and pack.sale_price_fcfa == conflict.common_price_fcfa and pack.base_quantity_milli == conflict.base_quantity_milli
    return {**pack_price_conflict_state(conflict, pack, product), "can_resolve": can_resolve,
        "preview_token": signing.dumps(_frame_digest(frame), salt=TOKEN_SALT) if can_resolve else None}


@transaction.atomic
def resolve_pack_price_conflict(shop_id, product_id, pack_id, conflict_id, actor, installation, validated_data, session_id):
    shop, product, pack = _context(shop_id, product_id, pack_id, actor, installation, session_id)
    data = _command(validated_data, ("operation_id", "preview_token", "proposal_id", "reason"))
    if data["proposal_id"] is not None:
        data["proposal_id"] = _uuid(data["proposal_id"])
    if not isinstance(data["preview_token"], str) or len(data["preview_token"]) > 512:
        raise ValidationError({"preview_token": _("Invalid preview.")})
    canonical = json_value({**data, "product_id": product.id, "pack_id": pack.id, "conflict_id": _uuid(conflict_id)})
    original = original_receipt(data["operation_id"], shop, actor, RESOLVE_ACTION, canonical)
    if original is not None:
        return original, False
    conflict = get_object_or_404(PackPriceConflict, shop=shop, product=product, pack=pack, id=conflict_id)
    if (conflict.state != "open" or conflict.version >= MAX_INTEGER
            or pack.sale_price_fcfa != conflict.common_price_fcfa or pack.base_quantity_milli != conflict.base_quantity_milli):
        raise OperationConflict()
    try:
        digest = signing.loads(data["preview_token"], salt=TOKEN_SALT)
    except (signing.BadSignature, ValueError, TypeError):
        raise OperationConflict()
    if digest != _frame_digest(_preview_frame(conflict, product, pack, actor, installation, session_id)):
        raise OperationConflict()
    proposal = conflict.proposals.filter(id=data["proposal_id"]).first() if data["proposal_id"] else None
    if data["proposal_id"] is not None and proposal is None:
        raise OperationConflict()
    price = proposal.proposed_price_fcfa if proposal else conflict.common_price_fcfa
    before, conflict_before = catalogue_state(product, pack), conflict_state(conflict)
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor, action=RESOLVE_ACTION,
        target_id=pack.id, reason=data["reason"], details={"request": canonical,
            "before": {"version": pack.version, "product_version": product.version, "sale_price_fcfa": pack.sale_price_fcfa}})
    decision = PackPriceResolution.objects.create(conflict=conflict, operation=event, proposal=proposal, actor_id=actor.id,
        installation_id=installation.id if installation else None, price_fcfa=price, reason=data["reason"])
    _advance_pack(product, pack, price)
    conflict.state = "resolved"
    conflict.version += 1
    conflict.save(update_fields=["state", "version", "updated_at"])
    receipt = pack_price_conflict_state(conflict, pack, product)
    snapshot = operation_snapshot(event=event, installation=installation, product=product, pack=pack,
        conflict=conflict, before=before, conflict_before=conflict_before, resolution=decision)
    receipt["operation_snapshot"] = snapshot
    event.details["operation_snapshot"] = snapshot
    event.details.update(after={"version": pack.version, "product_version": product.version, "sale_price_fcfa": price}, response=receipt)
    event.save(update_fields=["details", "updated_at"])
    return receipt, True
