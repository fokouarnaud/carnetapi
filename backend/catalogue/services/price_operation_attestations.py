"""Freeze only one native price operation's identities and catalogue delta."""
from common.services.operations import json_value


def catalogue_state(product, pack=None):
    """Select public price revisions and their actual persisted timestamps."""
    return json_value({"product": {name: getattr(product, name) for name in
        ("id", "version", "sale_price_fcfa", "updated_at")},
        "pack": None if pack is None else {name: getattr(pack, name) for name in
        ("id", "product_id", "version", "sale_price_fcfa", "base_quantity_milli", "updated_at")}})


def conflict_state(conflict):
    return None if conflict is None else {"state": conflict.state, "version": conflict.version}


def operation_snapshot(*, event, installation, product, conflict, before,
                       conflict_before, proposals=(), resolution=None, pack=None):
    """Preserve real UUIDs and source clocks without copying aggregate history.

    Call only for a new operation, after all its model saves in its transaction.
    Old receipts return before this builder and must never be reconstructed here.
    Source catalogue proofs can contain costs; they are deliberately excluded.
    """
    if len(proposals) > 2:
        raise ValueError("price_operation_delta_too_large")
    frozen_conflict = {name: getattr(conflict, name) for name in
        ("id", "source_change_id", "opening_operation_id", "base_version", "common_price_fcfa", "created_at")}
    if pack is not None:
        frozen_conflict["base_quantity_milli"] = conflict.base_quantity_milli
    return json_value({
        "schema_version": 1, "operation_type": event.action, "operation_id": event.id,
        "scope": {"shop_id": event.shop_id, "product_id": product.id,
                  "pack_id": None if pack is None else pack.id},
        "original_request": event.details["request"], "actor_id": event.actor_id,
        "installation_id": installation.id if installation else None, "recorded_at": event.created_at,
        "conflict": frozen_conflict, "conflict_before": conflict_before,
        "conflict_after": conflict_state(conflict), "catalogue_before": before,
        "catalogue_after": catalogue_state(product, pack),
        "proposals_created": [{name: getattr(row, name) for name in
            ("id", "operation_id", "actor_id", "installation_id", "is_native", "proposed_price_fcfa", "reason", "created_at")}
            for row in sorted(proposals, key=lambda row: (row.created_at, str(row.id)))],
        "resolution": None if resolution is None else {name: getattr(resolution, name) for name in
            ("id", "operation_id", "proposal_id", "actor_id", "installation_id", "price_fcfa", "reason", "created_at")},
    })
