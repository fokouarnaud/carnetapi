"""Audited product cost replay; never rewrite commercial, stock or cash facts."""
from django.db import transaction
from django.db.models import OuterRef, Subquery

from common.services.operations import current_shop, require_manager
from stock.models import CostRevaluation, CostRevaluationItem, ReturnCostReconstruction, ReturnCostRevision, ReturnCostSnapshot, ReturnReceipt, StockCostSnapshot
from purchasing.models import PurchaseChargeRevision
from stock.services.movement_costs import iter_movement_valuations, ordered_product_cost_events, movement_cost_fields
from stock.services.moving_average_cost import returned_cost_nanos


def _revise_returns(batch, movement, sale_cost, *, reconstruct_legacy_returns=False):
    for receipt, original, previous, current, values in planned_return_costs(movement, {
        "cost_state": sale_cost.cost_state, "total_cost_nanos_fcfa": sale_cost.total_cost_nanos_fcfa,
        "unit_cost_micros_numerator": sale_cost.unit_cost_micros_numerator,
        "unit_cost_micros_denominator": sale_cost.unit_cost_micros_denominator}, include_missing=reconstruct_legacy_returns):
        if original is None:
            micros = receipt.unit_cost_micros
            original = ReturnCostSnapshot.objects.create(receipt=receipt, sale_cost=None,
                cost_state="unknown" if micros is None else "provisional" if receipt.cost_is_provisional else "known",
                total_cost_nanos_fcfa=None if micros is None else micros * receipt.quantity_milli,
                unit_cost_micros_numerator=micros, unit_cost_micros_denominator=None if micros is None else 1)
            ReturnCostReconstruction.objects.create(original=original, batch=batch,
                previous_legacy_unit_cost_micros=micros, previous_cost_is_provisional=receipt.cost_is_provisional)
            current = original
        values["sale_cost_id"] = sale_cost.id
        if any(getattr(current, key) != value for key, value in values.items()):
            ReturnCostRevision.objects.create(origin=original, batch=batch,
                version=2 if previous is None else previous.version + 1, supersedes=previous, **values)


def planned_return_costs(movement, sale_values, *, include_missing=False):
    """Calculate receipt shares without writing a cost or inventing an old source."""
    if movement.sale_line_id is None:
        return
    before = 0
    receipts = ReturnReceipt.objects.filter(return_line__sale_line_id=movement.sale_line_id).select_related(
        "return_line", "cost_snapshot").order_by("created_at", "id")
    for receipt in receipts.iterator(chunk_size=1000):
        original = getattr(receipt, "cost_snapshot", None)
        if original is None and not include_missing:
            # Old receipts without a frozen cost retain their explicit legacy provenance.
            before += receipt.quantity_milli
            continue
        previous = original.revisions.order_by("-version").first() if original is not None else None
        current = previous or original
        values = {"cost_state": sale_values["cost_state"],
            "total_cost_nanos_fcfa": returned_cost_nanos(sale_values["total_cost_nanos_fcfa"],
                receipt.return_line.original_quantity_milli, before, receipt.quantity_milli),
            "unit_cost_micros_numerator": sale_values["unit_cost_micros_numerator"],
            "unit_cost_micros_denominator": sale_values["unit_cost_micros_denominator"]}
        was_provisional = (current is not None and current.cost_state == "provisional") or (
            original is None and receipt.unit_cost_micros is not None and receipt.cost_is_provisional)
        if was_provisional and values["cost_state"] == "known":
            values["cost_state"] = "provisional"
        yield receipt, original, previous, current, values
        before += receipt.quantity_milli


def movement_history(product):
    return ordered_product_cost_events(product.id)


def movement_cost_values(after, cost, previous, *, legacy_was_provisional=False):
    values = movement_cost_fields(after, cost)
    was_provisional = previous.cost_state == "provisional" if previous is not None else legacy_was_provisional
    if was_provisional and values["cost_state"] == "known":
        values["cost_state"] = "provisional"
        values["valuation_requires_regularization"] = True
    if previous is not None and previous.valuation_requires_regularization:
        values["valuation_requires_regularization"] = True
    return values


@transaction.atomic
def revalue_product_costs(batch, *, reconstruct_legacy_returns=False):
    """Apply one internal manager-authorized batch atomically, with a stable retry.

    The API caller validates the current session and approved preview before
    creating this batch. Internal callers must provide the same authorization.
    """
    batch = CostRevaluation.objects.select_related("product__shop", "operation__actor", "installation").get(pk=batch.pk)
    shop = current_shop(batch.product.shop_id, batch.operation.actor, batch.installation)
    require_manager(shop, batch.operation.actor)
    if (batch.operation.shop_id != shop.id or batch.operation.target_id != batch.product_id
            or batch.installation.user_id != batch.operation.actor_id or not batch.operation.reason.strip()):
        raise ValueError("Cost replay must identify its product, shop, manager, installation and reason.")
    if batch.operation.details.get("cost_revaluation_complete"):
        return batch
    latest = PurchaseChargeRevision.objects.filter(charge_id=OuterRef("charge_id")).order_by("-version").values("version")[:1]
    source = PurchaseChargeRevision.objects.filter(version=Subquery(latest),
        charge__revisions__allocations__line__product=batch.product).order_by("id").values_list("id", flat=True).distinct()
    batch.source_revision_ids = [str(identifier) for identifier in source.iterator(chunk_size=1000)]
    batch.save(update_fields=["source_revision_ids", "updated_at"])
    movements = movement_history(batch.product)
    ordered = movements
    for movement, after, cost in iter_movement_valuations(ordered):
        if cost is None:
            continue  # Value-only confirmations have their own immutable record, no physical movement.
        previous = StockCostSnapshot.objects.filter(movement=movement).order_by("-version").first()
        values = movement_cost_values(after, cost, previous, legacy_was_provisional=bool(
            movement.sale_line_id and movement.sale_line.sale.stock_requires_regularization))
        if previous is None or any(getattr(previous, key) != value for key, value in values.items()):
            revised = StockCostSnapshot.objects.create(movement=movement, trigger_operation=batch.operation,
                version=1 if previous is None else previous.version + 1, supersedes=previous, **values)
            legacy = movement.sale_line.unit_cost_micros if movement.sale_line_id else movement.unit_cost_micros
            CostRevaluationItem.objects.create(batch=batch, movement=movement, previous=previous, revised=revised,
                previous_legacy_unit_cost_micros=legacy if previous is None else None)
        else:
            revised = previous
        _revise_returns(batch, movement, revised, reconstruct_legacy_returns=reconstruct_legacy_returns)
    incomplete_returns = ReturnReceipt.objects.filter(return_line__sale_line__product=batch.product,
        cost_snapshot__isnull=True).count()
    batch.operation.details = {**batch.operation.details, "cost_revaluation_complete": True,
        "cost_revaluation_incomplete_returns": incomplete_returns}
    batch.operation.save(update_fields=["details", "updated_at"])
    return batch
