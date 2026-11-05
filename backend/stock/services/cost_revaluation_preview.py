"""Read-only replay summaries and a fingerprint of every valuation dependency."""
import hashlib
import json
from types import SimpleNamespace

from stock.models import CostRevaluation, NonSellableDecision, ReturnCostReconstruction, ReturnCostRevision, ReturnCostSnapshot, ReturnReceipt, StockCostSnapshot, StockCount, StockCountAdjustment, StockLoss, StockMovement, StockShortageReview, StockValuationAdjustment
from catalogue.models import Product
from purchasing.models import PurchaseChargeAllocation, PurchaseChargeRevision, PurchaseLine, PurchaseReceiptLine, SupplierReturnLine
from sales.models import SaleLine, SaleReturnLine
from stock.services.cost_revaluation import movement_history, movement_cost_values, planned_return_costs
from stock.services.movement_costs import iter_movement_valuations
from stock.services.return_costs import current_return_cost


def cost_input_fingerprint(product):
    digest = hashlib.sha256()
    receipt_filter = {"return_line__sale_line__product": product}
    sources = [
        Product.objects.filter(id=product.id),
        StockMovement.objects.filter(product=product),
        StockLoss.objects.filter(product=product),
        StockCount.objects.filter(product=product),
        StockCountAdjustment.objects.filter(count__product=product),
        StockValuationAdjustment.objects.filter(product=product),
        StockShortageReview.objects.filter(product=product),
        CostRevaluation.objects.filter(product=product),
        SaleLine.objects.filter(product=product),
        SaleReturnLine.objects.filter(sale_line__product=product),
        StockCostSnapshot.objects.filter(movement__product=product),
        PurchaseLine.objects.filter(product=product),
        PurchaseReceiptLine.objects.filter(movement__product=product),
        SupplierReturnLine.objects.filter(movement__product=product),
        PurchaseChargeAllocation.objects.filter(line__product=product),
        PurchaseChargeRevision.objects.filter(charge__revisions__allocations__line__product=product).distinct(),
        ReturnReceipt.objects.filter(**receipt_filter),
        ReturnCostSnapshot.objects.filter(receipt__return_line__sale_line__product=product),
        ReturnCostRevision.objects.filter(origin__receipt__return_line__sale_line__product=product),
        ReturnCostReconstruction.objects.filter(original__receipt__return_line__sale_line__product=product),
        NonSellableDecision.objects.filter(receipt__return_line__sale_line__product=product),
    ]
    for source in sources:
        # Persisted previews use the original model identity across app relocation.
        digest.update(f"core.{source.model.__name__}".encode())
        for row in source.order_by("id").values().iterator(chunk_size=1000):
            digest.update(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def _add_cost(totals, values):
    amount = values["total_cost_nanos_fcfa"]
    if amount is None:
        totals["unknown_lines"] += 1
    else:
        totals["total_nanos_fcfa"] += amount
    totals["provisional_lines"] += values["cost_state"] == "provisional"


def preview_cost_revaluation(product, *, reconstruct_legacy_returns=False):
    """Simulate current costs in memory; no temporary rows or rollback writes."""
    virtual_returns = {}
    def resolve(receipt):
        return virtual_returns[receipt.id] if receipt.id in virtual_returns else current_return_cost(receipt)
    before = {"total_nanos_fcfa": 0, "unknown_lines": 0, "provisional_lines": 0}
    after = dict(before)
    loss_before, loss_after = dict(before), dict(before)
    movement_changes = return_changes = reconstructed = reconstructed_returns = 0
    requires_regularization = False
    state = None
    history = movement_history(product)
    for movement, state, cost in iter_movement_valuations(history, return_cost_resolver=resolve):
        if cost is None:
            requires_regularization = requires_regularization or state.has_unresolved_valuation or state.has_unresolved_shortage
            continue
        previous = StockCostSnapshot.objects.filter(movement=movement).order_by("-version").first()
        values = movement_cost_values(state, cost, previous, legacy_was_provisional=bool(
            movement.sale_line_id and movement.sale_line.sale.stock_requires_regularization))
        requires_regularization = requires_regularization or values["valuation_requires_regularization"]
        changed = previous is None or any(getattr(previous, key) != value for key, value in values.items())
        movement_changes += changed
        reconstructed += previous is None
        if movement.kind in ("sale", "loss"):
            if previous is None:
                legacy = movement.sale_line.unit_cost_micros if movement.kind == "sale" else movement.unit_cost_micros
                source = movement.sale_line.sale if movement.kind == "sale" else getattr(movement, "loss", None)
                provisional = source is not None and source.stock_requires_regularization
                old = {"total_cost_nanos_fcfa": None if legacy is None else legacy * -movement.quantity_milli,
                    "cost_state": "unknown" if legacy is None else "provisional" if provisional else "known"}
            else:
                old = {key: getattr(previous, key) for key in ("total_cost_nanos_fcfa", "cost_state")}
            first, last = (before, after) if movement.kind == "sale" else (loss_before, loss_after)
            _add_cost(first, old)
            _add_cost(last, values)
        for receipt, original, _revision, current, returned in planned_return_costs(movement, values,
                include_missing=reconstruct_legacy_returns):
            reconstructed_returns += original is None
            source_changed = original is None or changed or current.sale_cost_id != previous.id
            return_changes += source_changed or any(getattr(current, key) != value for key, value in returned.items())
            virtual_returns[receipt.id] = SimpleNamespace(**returned)
    for totals in (before, after, loss_before, loss_after):
        totals["total_nanos_fcfa"] = None if totals["unknown_lines"] else str(totals["total_nanos_fcfa"])
    fingerprint = cost_input_fingerprint(product)
    if reconstruct_legacy_returns:
        fingerprint = hashlib.sha256((fingerprint + ":reconstruct_legacy_returns").encode()).hexdigest()
    return {"preview_token": fingerprint, "movement_changes": movement_changes,
        "return_changes": return_changes, "reconstructed_movements": reconstructed,
        "reconstruct_legacy_returns": reconstruct_legacy_returns, "reconstructed_return_receipts": reconstructed_returns,
        "legacy_return_receipts": ReturnReceipt.objects.filter(return_line__sale_line__product=product,
            cost_snapshot__isnull=True).count(),
        "sales_cost_before": before, "sales_cost_after": after,
        "loss_cost_before": loss_before, "loss_cost_after": loss_after,
        "valuation_requires_regularization": requires_regularization,
        "quantity_after_milli": str(state.quantity_milli if state else 0),
        "stock_value_after_nanos_fcfa": None if state and state.stock_value_nanos_fcfa is None else str(state.stock_value_nanos_fcfa if state else 0)}
