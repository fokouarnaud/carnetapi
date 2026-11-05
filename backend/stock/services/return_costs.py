"""Allocate original costs to physical returns without rounding to an integer unit price."""
from stock.models import ReturnCostSnapshot, ReturnReceipt, StockCostSnapshot
from stock.services.moving_average_cost import returned_cost_nanos


def receipt_cost_inputs(line, quantity):
    """Use the latest audited sale cost, falling back only when no snapshot exists."""
    origin = StockCostSnapshot.objects.filter(movement__sale_line=line.sale_line).order_by("-version").first()
    if origin is None:
        micros = line.sale_line.unit_cost_micros
        total = None if micros is None else micros * line.original_quantity_milli
        state = "unknown" if micros is None else "provisional" if line.sale_line.sale.stock_requires_regularization else "known"
        numerator, denominator = micros, None if micros is None else 1
    else:
        total, state = origin.total_cost_nanos_fcfa, origin.cost_state
        numerator, denominator = origin.unit_cost_micros_numerator, origin.unit_cost_micros_denominator
    before = sum(ReturnReceipt.objects.filter(return_line__sale_line=line.sale_line).values_list("quantity_milli", flat=True).iterator(chunk_size=1000))
    return {"sale_cost": origin, "cost_state": state,
        "total_cost_nanos_fcfa": returned_cost_nanos(total, line.original_quantity_milli, before, quantity),
        "unit_cost_micros_numerator": numerator, "unit_cost_micros_denominator": denominator}


def integral_unit_cost(values):
    """Project a legacy micro-FCFA integer only when it represents the exact cost."""
    numerator, denominator = values["unit_cost_micros_numerator"], values["unit_cost_micros_denominator"]
    if numerator is None:
        return None
    quotient, remainder = divmod(numerator, denominator)
    return quotient if remainder == 0 and quotient <= 9223372036854775807 else None


def current_return_cost(receipt):
    """Read the latest explicit revision while preserving the original receipt record."""
    original = getattr(receipt, "cost_snapshot", None)
    if original is None:
        # A replay may have reconstructed this baseline after the movement cursor opened.
        original = ReturnCostSnapshot.objects.filter(receipt_id=receipt.id).first()
        if original is None:
            return None
    return original.revisions.order_by("-version").first() or original


def decision_cost(decision, *, cost_resolver=current_return_cost):
    """Allocate the receipt cost across all explicit dispositions, including scrap."""
    receipt = decision.receipt
    cost = cost_resolver(receipt)
    if cost is None:
        return None, None
    before = receipt.quantity_milli - decision.after_quantity_milli - decision.quantity_milli
    total = returned_cost_nanos(cost.total_cost_nanos_fcfa, receipt.quantity_milli, before, decision.quantity_milli)
    return cost, total
