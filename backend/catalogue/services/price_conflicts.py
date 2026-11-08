"""Product-price conflicts preserve evidence and require an explicit decision."""
import hashlib
import json
import uuid

from django.core import signing
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from catalogue.models import Product, ProductPriceConflict, ProductPriceProposal, ProductPriceResolution
from common.models import Activity
from common.services.errors import OperationConflict
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from identity.services.authentication import require_live_session
from catalogue.services.price_operation_attestations import catalogue_state, conflict_state, operation_snapshot

MAX_INTEGER = 9223372036854775807
RECORD_ACTION = "catalogue_price_conflict_recorded"
RESOLVE_ACTION = "catalogue_price_resolved"
TOKEN_SALT = "catalogue.product-price-conflict.v1"


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError(_("Invalid UUID."))


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise ValidationError(_("An exact integer within the supported range is required."))
    return value


def _command(data, required, optional=()):
    data = dict(data)
    if set(data) - set(required) - set(optional) or set(required) - set(data):
        raise ValidationError(_("Invalid request fields."))
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValidationError({"reason": _("A reason of 1 to 2000 characters is required.")})
    data["reason"] = reason.strip()
    data["operation_id"] = _uuid(data["operation_id"])
    return data


def _context(shop_id, product_id, actor, installation, session_id):
    require_live_session(actor, session_id, installation)
    shop = current_shop(shop_id, actor, installation)
    require_manager(shop, actor)
    return shop, get_object_or_404(Product, id=product_id, shop=shop)


def ensure_price_uncontested(product_id):
    """Reject a new price write while a manager's decision is still pending."""
    if ProductPriceConflict.objects.filter(product_id=product_id, state="open").exists():
        raise OperationConflict()


def price_conflict_state(conflict, product=None):
    """Expose price evidence without the source's private cost snapshots."""
    product = product or conflict.product
    if product.id != conflict.product_id or product.shop_id != conflict.shop_id:
        raise OperationConflict()
    try:
        decision = conflict.resolution
    except ProductPriceResolution.DoesNotExist:
        decision = None
    return json_value({
        "id": conflict.id, "product_id": conflict.product_id,
        "source_change_id": conflict.source_change_id, "base_version": conflict.base_version,
        "common_price_fcfa": conflict.common_price_fcfa, "state": conflict.state,
        "version": conflict.version, "product_version": product.version, "created_at": conflict.created_at,
        "proposals": [{"id": row.id, "operation_id": row.operation_id, "actor_id": row.actor_id,
            "installation_id": row.installation_id, "proposed_price_fcfa": row.proposed_price_fcfa,
            "reason": row.reason, "created_at": row.created_at} for row in conflict.proposals.all()],
        "resolution": None if decision is None else {"operation_id": decision.operation_id,
            "proposal_id": decision.proposal_id, "price_fcfa": decision.price_fcfa,
            "actor_id": decision.actor_id, "reason": decision.reason, "created_at": decision.created_at},
    })


def _source_proof(source, product, base_version):
    """Accept only an exact journalled transition of this product's price field."""
    details = source.details
    if not isinstance(details, dict):
        raise OperationConflict()
    request, before, after = (details.get(key) for key in ("request", "before", "after"))
    if not all(isinstance(value, dict) for value in (request, before, after)):
        raise OperationConflict()
    if (not isinstance(source.reason, str) or not source.reason.strip()
            or source.action != "product_updated" or source.target_id != product.id
            or source.shop_id != product.shop_id or details.get("response") != after
            or request.get("operation_id") != str(source.id) or request.get("product_id") != str(product.id)
            or type(request.get("base_version")) is not int or request["base_version"] != base_version
            or type(before.get("version")) is not int or before["version"] != base_version
            or type(after.get("version")) is not int or after["version"] != base_version + 1):
        raise OperationConflict()
    for snapshot in (before, after):
        if (snapshot.get("id") != str(product.id) or snapshot.get("shop_id") != str(product.shop_id)
                or type(snapshot.get("sale_price_fcfa")) is not int
                or not 0 <= snapshot["sale_price_fcfa"] <= MAX_INTEGER):
            raise OperationConflict()
    if (type(request.get("sale_price_fcfa")) is not int
            or request["sale_price_fcfa"] != after["sale_price_fcfa"]
            or before["sale_price_fcfa"] == after["sale_price_fcfa"]):
        raise OperationConflict()
    # A restored writer is the current reviewer; preserve the source identities
    # as references, without creating or reactivating authentication identities.
    actor_id, installation_id = source.actor_id, None
    try:
        capture = source.recovered_origin.capture
    except ObjectDoesNotExist:
        capture = None
    if capture is not None:
        if (capture.shop_id != source.shop_id or capture.operation_id != source.id
                or capture.operation_type != "product_update"):
            raise OperationConflict()
        actor_id, installation_id = capture.origin_user_id, capture.origin_installation_id
    return json_value({"request": request, "before": before, "after": after,
        "actor_id": actor_id, "installation_id": installation_id, "reason": source.reason})


def _advance_product(product, price):
    if product.version >= MAX_INTEGER:
        raise OperationConflict()
    product.sale_price_fcfa = price
    product.version += 1
    product.save(update_fields=["sale_price_fcfa", "version", "updated_at"])


@transaction.atomic
def record_price_conflict(shop_id, product_id, actor, installation, validated_data, session_id):
    """Preserve competing proposals and resume sales at the proven common price."""
    shop, product = _context(shop_id, product_id, actor, installation, session_id)
    data = _command(validated_data, ("operation_id", "source_change_id", "base_version", "proposed_price_fcfa", "reason"))
    data["source_change_id"] = _uuid(data["source_change_id"])
    _integer(data["base_version"], 1)
    _integer(data["proposed_price_fcfa"])
    canonical = json_value({**data, "product_id": product.id})
    original = original_receipt(data["operation_id"], shop, actor, RECORD_ACTION, canonical)
    if original is not None:
        return original, False
    source = get_object_or_404(Activity, id=data["source_change_id"], shop=shop)
    proof = _source_proof(source, product, data["base_version"])
    conflict = ProductPriceConflict.objects.filter(product=product, state="open").first()
    if conflict is not None:
        if (conflict.shop_id != shop.id or conflict.source_change_id != source.id or conflict.source_snapshot != proof
                or conflict.base_version != data["base_version"] or product.sale_price_fcfa != conflict.common_price_fcfa
                or product.version < conflict.base_version + 2
                or conflict.version >= MAX_INTEGER):
            raise OperationConflict()
    elif (product.version != proof["after"]["version"]
            or product.sale_price_fcfa != proof["after"]["sale_price_fcfa"]
            or ProductPriceConflict.objects.filter(source_change=source).exists()):
        raise OperationConflict()
    before, conflict_before = catalogue_state(product), conflict_state(conflict)
    proposals_created = []
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor, action=RECORD_ACTION,
        target_id=product.id, reason=data["reason"], details={"request": canonical,
            "before": {"version": product.version, "sale_price_fcfa": product.sale_price_fcfa}})
    if conflict is None:
        conflict = ProductPriceConflict.objects.create(shop=shop, product=product, source_change=source,
            opening_operation=event, base_version=data["base_version"],
            common_price_fcfa=proof["before"]["sale_price_fcfa"], source_snapshot=proof)
        proposals_created.append(ProductPriceProposal.objects.create(conflict=conflict, operation=source, actor_id=proof["actor_id"],
            installation_id=proof["installation_id"], is_native=True,
            proposed_price_fcfa=proof["after"]["sale_price_fcfa"], reason=source.reason))
        _advance_product(product, conflict.common_price_fcfa)
    else:
        conflict.version += 1
        conflict.save(update_fields=["version", "updated_at"])
    proposals_created.append(ProductPriceProposal.objects.create(conflict=conflict, operation=event, actor_id=actor.id,
        installation_id=installation.id if installation else None,
        proposed_price_fcfa=data["proposed_price_fcfa"], reason=data["reason"]))
    receipt = price_conflict_state(conflict, product)
    snapshot = operation_snapshot(event=event, installation=installation, product=product,
        conflict=conflict, before=before, conflict_before=conflict_before, proposals=proposals_created)
    receipt["operation_snapshot"] = snapshot
    event.details["operation_snapshot"] = snapshot
    event.details.update(after={"version": product.version, "sale_price_fcfa": product.sale_price_fcfa}, response=receipt)
    event.save(update_fields=["details", "updated_at"])
    return receipt, True


def _preview_frame(conflict, product, actor, installation, session_id):
    return {"receipt": price_conflict_state(conflict, product), "price_fcfa": product.sale_price_fcfa,
        "actor_id": str(actor.id), "installation_id": str(installation.id) if installation else None,
        "session_id": str(session_id)}


def _frame_digest(frame):
    # The token stays bounded even when a conflict preserves many proposals.
    return hashlib.sha256(json.dumps(frame, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


@transaction.atomic
def preview_price_conflict(shop_id, product_id, conflict_id, actor, installation, session_id):
    """Bind a review to the current proposals and the manager's live session."""
    shop, product = _context(shop_id, product_id, actor, installation, session_id)
    conflict = get_object_or_404(ProductPriceConflict, id=conflict_id, product=product, shop=shop)
    frame = _preview_frame(conflict, product, actor, installation, session_id)
    can_resolve = conflict.state == "open" and product.sale_price_fcfa == conflict.common_price_fcfa
    return {**frame["receipt"], "can_resolve": can_resolve,
        "preview_token": signing.dumps(_frame_digest(frame), salt=TOKEN_SALT) if can_resolve else None}


@transaction.atomic
def resolve_price_conflict(shop_id, product_id, conflict_id, actor, installation, validated_data, session_id):
    """Choose a preserved proposal or the common price, never an arbitrary amount."""
    shop, product = _context(shop_id, product_id, actor, installation, session_id)
    data = _command(validated_data, ("operation_id", "preview_token", "proposal_id", "reason"))
    if data["proposal_id"] is not None:
        data["proposal_id"] = _uuid(data["proposal_id"])
    if not isinstance(data["preview_token"], str) or len(data["preview_token"]) > 512:
        raise ValidationError({"preview_token": _("Invalid preview.")})
    canonical = json_value({**data, "product_id": product.id, "conflict_id": _uuid(conflict_id)})
    original = original_receipt(data["operation_id"], shop, actor, RESOLVE_ACTION, canonical)
    if original is not None:
        return original, False
    conflict = get_object_or_404(ProductPriceConflict, id=conflict_id, product=product, shop=shop)
    if conflict.state != "open" or product.sale_price_fcfa != conflict.common_price_fcfa or conflict.version >= MAX_INTEGER:
        raise OperationConflict()
    try:
        frame = signing.loads(data["preview_token"], salt=TOKEN_SALT)
    except (signing.BadSignature, ValueError, TypeError):
        raise OperationConflict()
    if frame != _frame_digest(_preview_frame(conflict, product, actor, installation, session_id)):
        raise OperationConflict()
    proposal = None
    if data["proposal_id"] is not None:
        proposal = conflict.proposals.filter(id=data["proposal_id"]).first()
        if proposal is None:
            raise OperationConflict()
    price = proposal.proposed_price_fcfa if proposal else conflict.common_price_fcfa
    before, conflict_before = catalogue_state(product), conflict_state(conflict)
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor, action=RESOLVE_ACTION,
        target_id=product.id, reason=data["reason"], details={"request": canonical,
            "before": {"version": product.version, "sale_price_fcfa": product.sale_price_fcfa}})
    decision = ProductPriceResolution.objects.create(conflict=conflict, operation=event, proposal=proposal,
        actor_id=actor.id, installation_id=installation.id if installation else None,
        price_fcfa=price, reason=data["reason"])
    _advance_product(product, price)
    conflict.state = "resolved"
    conflict.version += 1
    conflict.save(update_fields=["state", "version", "updated_at"])
    receipt = price_conflict_state(conflict, product)
    snapshot = operation_snapshot(event=event, installation=installation, product=product,
        conflict=conflict, before=before, conflict_before=conflict_before, resolution=decision)
    receipt["operation_snapshot"] = snapshot
    event.details["operation_snapshot"] = snapshot
    event.details.update(after={"version": product.version, "sale_price_fcfa": price}, response=receipt)
    event.save(update_fields=["details", "updated_at"])
    return receipt, True
