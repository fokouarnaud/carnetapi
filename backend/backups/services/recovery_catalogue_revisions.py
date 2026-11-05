"""Recover catalogue revisions only against the exact prior confirmed state."""
import hashlib

from django.utils import timezone

from backups.journal_protocol import canonical_bytes
from backups.models import RecoveredOperationOrigin
from backups.services.recovery_catalogue_snapshots import decode_catalogue_revision, immutable_catalogue_state
from backups.services.recovery_customers import recorded_origin_evidence
from catalogue.api.catalogue_queries import with_codes
from catalogue.api.catalogue_serializers import CostProductSerializer, PackSerializer
from catalogue.models import Category, Product, ProductPack
from common.models import Activity
from common.services.errors import OperationConflict
from common.services.operations import json_value
from identity.models import Installation
from stock.services.stock_alert_services import observe_stock
from stock.services.stock_queries import with_stock


def _state(item, *, pack):
    if item is None:return None
    values = {name: getattr(item, name) for name in (
        "id", "name", "sale_price_fcfa", "is_active", "version", "created_at", "updated_at")}
    if pack:values.update(product_id=item.product_id, product_version=item.product.version,
                          product_updated_at=item.product.updated_at, base_quantity_milli=item.base_quantity_milli)
    else:values.update(shop_id=item.shop_id, category_id=item.category_id, unit_kind=item.unit_kind,
        unit_label=item.unit_label, purchase_price_fcfa=item.purchase_price_fcfa,
        stock_threshold_milli=item.stock_threshold_milli)
    return json_value(values)


def _current(shop, snapshot, *, pack):
    if pack:
        return ProductPack.objects.select_related("product").filter(product__shop=shop,
            product_id=snapshot["before"]["product_id"], id=snapshot["before"]["id"]).first()
    return Product.objects.filter(shop=shop, id=snapshot["before"]["id"]).first()


def _known(capture, decoded, current, issues, notices, *, pack):
    operation = Activity.objects.filter(shop_id=capture.shop_id, id=capture.operation_id).first()
    if operation is None:
        if Activity.objects.filter(id=capture.operation_id).exists():
            issues.append({"code": "operation_conflict", "ids": [str(capture.operation_id)]})
        return None, False
    verified = False
    try:
        if not isinstance(operation.details, dict):raise ValueError()
        actor, device, verified, _origin = recorded_origin_evidence(capture, operation, notices, client_metadata=True)
        matches = (current is not None and operation.target_id == decoded.snapshot["after"]["id"]
            and operation.action == ("pack_updated" if pack else "product_updated")
            and actor == capture.origin_user_id and device
            and operation.details.get("request") == decoded.canonical_request
            and immutable_catalogue_state(operation.details["before"], pack=pack) == json_value(decoded.snapshot["before"])
            and immutable_catalogue_state(operation.details["response"], pack=pack) == json_value(decoded.snapshot["after"]))
    except (ValueError, KeyError, TypeError, AttributeError):matches = False
    if not matches:issues.append({"code": "operation_conflict", "ids": [str(capture.operation_id)]})
    return operation, verified


def build_catalogue_revision_preview(shop, capture):
    pack = capture.operation_type == "pack_update"
    decoded = decode_catalogue_revision(capture)
    current = _current(shop, decoded.snapshot, pack=pack)
    state = _state(current, pack=pack)
    issues, notices = [], []
    known, verified = _known(capture, decoded, current, issues, notices, pack=pack)
    if not verified and not notices:notices.append({"code": "declared_origin_installation", "ids": []})
    if known is None:
        if capture.occurred_at > timezone.now():issues.append({"code": "future_effective_date", "ids": []})
        if pack and decoded.snapshot["after"]["product_updated_at"] > timezone.now():
            issues.append({"code": "future_parent_revision_date", "ids": []})
        if current is None:
            issues.append({"code": "missing_pack" if pack else "missing_product", "ids": [str(decoded.snapshot["before"]["id"])]})
        elif state != json_value(decoded.snapshot["before"]):
            issues.append({"code": "pack_revision_conflict" if pack else "product_revision_conflict", "ids": [str(current.id)]})
        if not pack and current is not None and "sale_price_fcfa" in decoded.request:
            from catalogue.services.price_conflicts import ensure_price_uncontested
            try:ensure_price_uncontested(current.id)
            except OperationConflict:
                issues.append({"code":"catalogue_price_conflict","ids":[str(current.id)]})
        if pack and current is not None and current.product.unit_kind == "piece" and decoded.snapshot["after"]["base_quantity_milli"] % 1000:
            issues.append({"code": "product_unit_conflict", "ids": [str(current.product_id)]})
        if not pack and decoded.snapshot["after"]["category_id"] is not None and not Category.objects.filter(
                shop=shop, id=decoded.snapshot["after"]["category_id"]).exists():
            issues.append({"code": "missing_category", "ids": [str(decoded.snapshot["after"]["category_id"])]})
        ready = {str(value) for value in Activity.objects.filter(shop=shop, id__in=capture.dependencies,
            details__has_key="response").values_list("id", flat=True)}
        missing = sorted(set(capture.dependencies) - ready)
        if missing:issues.append({"code": "missing_dependencies", "ids": missing})
    device = Installation.objects.filter(id=capture.origin_installation_id).values("user_id").first()
    if device and device["user_id"] != capture.origin_user_id:
        issues.append({"code": "origin_identity_conflict", "ids": []})
    latest = capture.integration_decisions.order_by("-version").first()
    version = latest.version if latest else 0
    frame = json_value({"capture_id": capture.id, "source_sha256": capture.content_sha256, "version": version,
        "current_state": state, "known_receipt": known.details.get("response") if known and isinstance(known.details, dict) else None,
        "origin_installation_verified": verified, "issues": issues, "notices": notices})
    token = hashlib.sha256(canonical_bytes(frame)).hexdigest()
    preview = json_value({"capture_id": capture.id, "operation_type": capture.operation_type, "version": version,
        "preview_token": token, "can_apply": not issues, "outcome": "blocked" if issues else "already_known" if known else "new",
        "hold_id": None, "review_state": None, "review_version": 0, "can_hold": False, "can_refuse": False,
        "snapshot": decoded.snapshot, "current_state": state, "stocks": [], "cash_delta_by_method_fcfa": {},
        "new_debt_fcfa": 0, "new_customer_credit_fcfa": 0, "origin_installation_verified": verified,
        "notices": notices, "issues": issues, "origin_user_id": capture.origin_user_id,
        "origin_installation_id": capture.origin_installation_id, "origin_recorded_at": capture.origin_recorded_at})
    return preview, decoded, known, {}, current


def persist_catalogue_revision(*, shop, actor, installation, decoded, origin_capture, recovery_review):
    """Persist already reviewed facts within the integration caller's transaction."""
    pack = origin_capture.operation_type == "pack_update"
    item = _current(shop, decoded.snapshot, pack=pack)
    if _state(item, pack=pack) != json_value(decoded.snapshot["before"]):raise OperationConflict()
    if not pack and "sale_price_fcfa" in decoded.request:
        from catalogue.services.price_conflicts import ensure_price_uncontested
        ensure_price_uncontested(item.id)
    before = _state(item, pack=pack)
    data = dict(decoded.request)
    operation_id, reason = data.pop("operation_id"), data.pop("reason")
    data.pop("base_version")
    for name, value in data.items():setattr(item, name, value)
    item.version += 1
    item.save(update_fields=[*data, "version", "updated_at"])
    model = ProductPack if pack else Product
    model.objects.filter(id=item.id).update(updated_at=decoded.snapshot["after"]["updated_at"])
    if pack:
        product = item.product
        product.version += 1
        product.save(update_fields=["version", "updated_at"])
        Product.objects.filter(id=product.id).update(updated_at=decoded.snapshot["after"]["product_updated_at"])
        item = with_codes(ProductPack.objects.select_related("product"), pack=True).get(id=item.id)
        receipt = json_value(PackSerializer(item).data)
    else:
        item = with_codes(with_stock(Product.objects.all())).get(id=item.id)
        receipt = json_value(CostProductSerializer(item).data)
    event = Activity.objects.create(id=operation_id, shop=shop, actor=actor,
        action="pack_updated" if pack else "product_updated", target_id=item.id, reason=reason,
        details={"request": decoded.canonical_request, "before": before, "after": receipt, "response": receipt,
            "installation_id": str(installation.id), "recovery": json_value({"capture_id": origin_capture.id,
                "origin_user_id": origin_capture.origin_user_id, "origin_installation_id": origin_capture.origin_installation_id,
                "origin_recorded_at": origin_capture.origin_recorded_at, "origin_installation_verified": False,
                "source_snapshot": decoded.snapshot, "review": recovery_review})})
    RecoveredOperationOrigin.objects.create(operation=event, capture=origin_capture)
    if not pack:observe_stock([item.id], event)
    return receipt
