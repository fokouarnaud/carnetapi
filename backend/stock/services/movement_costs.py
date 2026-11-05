"""Read immutable movement sources and append an initial exact valuation."""
from fractions import Fraction
from dataclasses import replace
import heapq
import hashlib
import json
from itertools import groupby

from django.db import transaction
from django.db.models import Case, When, Value, IntegerField, Exists, OuterRef

from stock.models import StockCostSnapshot, StockMovement, StockValuationAdjustment, StockShortageReview
from stock.services.moving_average_cost import AllocatedCost, AverageCostState, receipt_cost_nanos
from stock.services.return_costs import decision_cost, current_return_cost
from purchasing.services.purchase_costs import invoice_line_cost_fcfa


def causal_movement_order(movements):
    """Keep receipt priority while respecting each return's physical origin.

    Input is stably ordered by effective time, receipt priority and recording
    order. Only one timestamp's movements need to be held in memory.
    """
    for _instant, simultaneous in groupby(movements, key=lambda row: row.occurred_at):
        rows = list(simultaneous)
        sales = {row.sale_line_id: index for index, row in enumerate(rows) if row.sale_line_id}
        positions = {row.id: index for index, row in enumerate(rows)}
        children = [[] for _ in rows]
        pending = [0] * len(rows)
        for index, row in enumerate(rows):
            receipt = getattr(row, "return_receipt", None)
            decision = getattr(row, "non_sellable_decision", None)
            if decision is not None:
                receipt = decision.receipt
            parents = set()
            if receipt is not None:
                origin = sales.get(receipt.return_line.sale_line_id)
                if origin is not None:
                    parents.add(origin)
                # A later disposition also depends on a sellable receipt when present.
                if decision is not None and receipt.movement_id in positions:
                    parents.add(positions[receipt.movement_id])
            for parent in parents:
                children[parent].append(index)
                pending[index] += 1
        ready = [index for index, count in enumerate(pending) if count == 0]
        heapq.heapify(ready)
        emitted = 0
        while ready:
            index = heapq.heappop(ready)
            yield rows[index]
            emitted += 1
            for child in children[index]:
                pending[child] -= 1
                if pending[child] == 0:
                    heapq.heappush(ready, child)
        if emitted != len(rows):
            raise ValueError("Cyclic physical origin in cost history.")


def iter_movement_valuations(movements, *, return_cost_resolver=current_return_cost, history_digest=None, physical_digest=None):
    """Yield costs incrementally so an audited replay can update dependent returns."""
    state = AverageCostState()
    digest = history_digest if history_digest is not None else hashlib.sha256()
    physical = physical_digest if physical_digest is not None else hashlib.sha256()
    product_id = None
    received = {}
    supplier_returned = {}
    purchase_costs = {}
    seen = set()
    for movement in movements:
        identity = (movement.kind if movement.kind in {"valuation_adjustment", "shortage_review"} else "movement", movement.pk)
        if identity in seen:
            raise ValueError("Duplicate physical movement in cost history.")
        seen.add(identity)
        if product_id is None:
            product_id = movement.product_id
        if movement.product_id != product_id or movement.operation.shop_id != movement.product.shop_id:
            raise ValueError("Cost history must belong to one product and shop.")
        if movement.kind == "shortage_review":
            valid = (movement.basis_fingerprint == physical.hexdigest() and
                movement.stock_quantity_milli == state.quantity_milli and state.quantity_milli >= 0)
            movement.basis_matches = valid
            state = replace(state, has_unresolved_shortage=not valid)
            digest.update(json.dumps([str(movement.id), movement.basis_fingerprint, "shortage_review", valid],
                separators=(",", ":")).encode())
            yield movement, state, None
            continue
        if movement.kind == "valuation_adjustment":
            valid = movement.basis_fingerprint == digest.hexdigest() and movement.stock_quantity_milli == state.quantity_milli
            movement.basis_matches = valid
            quantity = state.quantity_milli
            value = movement.value_fcfa * 1000000000 if valid else None
            state = AverageCostState(quantity, max(quantity, 0), value if quantity > 0 else 0, 0,
                Fraction(value, quantity) if valid and quantity > 0 else state.last_known_unit_cost_micros,
                state.has_unresolved_shortage or quantity < 0, False, not valid)
            digest.update(json.dumps([str(movement.id), movement.basis_fingerprint, movement.value_fcfa, valid],
                separators=(",", ":")).encode())
            yield movement, state, None
            continue
        quantity = movement.quantity_milli
        if quantity == 0 and movement.kind == "opening":
            # A declared empty opening has no units to contribute to a CUMP.
            continue
        if quantity < 0 and movement.kind == "supplier_return":
            source_return = getattr(movement, "supplier_return_line", None)
            if source_return is None:
                state, cost = state.remove_at_cost(-quantity, None)
            else:
                source = source_return.line
                prior = supplier_returned.get(source.id, 0)
                if source.id not in purchase_costs:
                    purchase_costs[source.id] = invoice_line_cost_fcfa(source)
                original = purchase_costs[source.id]
                total = receipt_cost_nanos(original, source.base_quantity_milli, prior, -quantity)
                supplier_returned[source.id] = prior - quantity
                state, cost = state.remove_at_cost(-quantity, total)
                cost = replace(cost, unit_cost_micros=Fraction(original * 1000000000, source.base_quantity_milli))
        elif quantity < 0:
            state, cost = state.remove(-quantity)
        elif quantity > 0 and movement.kind == "inventory_adjustment":
            state, cost = state.receive_at_average(quantity)
        elif quantity > 0:
            provisional = False
            receipt_line = getattr(movement, "purchase_receipt_line", None)
            transferred = getattr(movement, "transfer_receipt_line", None)
            if movement.kind == "transfer_in" and transferred is not None:
                total = transferred.total_cost_nanos_fcfa
                provisional = transferred.cost_state == "provisional"
            elif movement.kind == "purchase_receipt" and receipt_line is not None:
                source = receipt_line.line
                prior = received.get(source.id, 0)
                if source.id not in purchase_costs:
                    purchase_costs[source.id] = invoice_line_cost_fcfa(source)
                total = receipt_cost_nanos(purchase_costs[source.id], source.base_quantity_milli, prior, quantity)
                received[source.id] = prior + quantity
            else:
                total = None if movement.unit_cost_micros is None else movement.unit_cost_micros * quantity
                returned = getattr(movement, "return_receipt", None)
                if returned is not None:
                    provisional = returned.cost_is_provisional
                    exact = return_cost_resolver(returned)
                    if exact is not None:
                        total, provisional = exact.total_cost_nanos_fcfa, exact.cost_state != "known"
                decision = getattr(movement, "non_sellable_decision", None)
                if decision is not None:
                    provisional = decision.receipt.cost_is_provisional
                    exact, allocated = decision_cost(decision, cost_resolver=return_cost_resolver)
                    if exact is not None:
                        total, provisional = allocated, exact.cost_state != "known"
            state = state.receive(quantity, total, is_provisional=provisional)
            basis = None if total is None else Fraction(total, quantity)
            cost = AllocatedCost(total, basis, "unknown" if total is None else "provisional" if provisional else "known", 0)
        else:
            raise ValueError("A physical movement must have nonzero quantity.")
        if getattr(movement, "had_valuation_gap", False):
            # Recalculation cannot substitute for a manager's explicit gap closure.
            state = replace(state, has_unresolved_valuation=True)
        historical_shortage = bool(movement.sale_line_id and movement.sale_line.sale.stock_requires_regularization)
        loss = getattr(movement, "loss", None) if movement.kind == "loss" else None
        if historical_shortage or (loss is not None and loss.stock_requires_regularization):
            state = replace(state, has_unresolved_shortage=True)
        physical.update(json.dumps([str(movement.id), movement.kind, quantity, movement.occurred_at.isoformat(),
            historical_shortage or bool(loss is not None and loss.stock_requires_regularization)],
            separators=(",", ":")).encode())
        digest.update(json.dumps([str(movement.id), movement.kind, quantity, movement.occurred_at.isoformat(),
            cost.total_nanos_fcfa, str(cost.unit_cost_micros), cost.state], separators=(",", ":")).encode())
        yield movement, state, cost


def value_ordered_movements(movements):
    """Value a single product's complete history in caller-supplied effective order."""
    valuations = list(iter_movement_valuations(movements))
    return (valuations[-1][1] if valuations else AverageCostState()), valuations


def movement_cost_fields(after, cost):
    """Keep captured and replayed snapshots consistent, including pending valuation gaps."""
    basis = cost.unit_cost_micros
    return {"cost_state": cost.state, "total_cost_nanos_fcfa": cost.total_nanos_fcfa,
        "unit_cost_micros_numerator": None if basis is None else basis.numerator,
        "unit_cost_micros_denominator": None if basis is None else basis.denominator,
        "quantity_after_milli": after.quantity_milli, "stock_value_after_nanos_fcfa": after.stock_value_nanos_fcfa,
        "valuation_requires_regularization": after.has_unresolved_valuation or after.has_unresolved_shortage or cost.state != "known"
            or after.stock_value_nanos_fcfa is None, "valuation_gap_nanos_fcfa": cost.valuation_gap_nanos_fcfa}


@transaction.atomic
def freeze_movement_cost(movement, after, cost):
    """Preserve one original valuation; a changed replay requires an explicit revision."""
    if movement.quantity_milli == 0:
        raise ValueError("A physical movement must have nonzero quantity.")
    if movement.operation.shop_id != movement.product.shop_id:
        raise ValueError("Movement and operation must belong to the same shop.")
    values = movement_cost_fields(after, cost)
    original = StockCostSnapshot.objects.filter(movement=movement, version=1).first()
    if original is not None:
        if any(getattr(original, key) != value for key, value in values.items()):
            raise ValueError("Original cost is already frozen; append a reviewed revision instead.")
        return original, False
    return StockCostSnapshot.objects.create(movement=movement, trigger_operation=movement.operation,
        version=1, **values), True


def ordered_product_movements(product_id, *, until=None):
    """Read a single product's physical sources in stable effective order."""
    movements = StockMovement.objects.filter(product_id=product_id)
    if until is not None:
        movements = movements.filter(occurred_at__lte=until)
    return movements.select_related(
        "product", "operation", "sale_line__sale", "loss", "purchase_receipt_line__line", "return_receipt__cost_snapshot",
        "supplier_return_line__line",
        "transfer_receipt_line",
        "return_receipt__return_line", "non_sellable_decision__receipt__cost_snapshot", "non_sellable_decision__receipt__return_line",
    ).annotate(had_valuation_gap=Exists(StockCostSnapshot.objects.filter(
        movement_id=OuterRef("pk"), valuation_gap_nanos_fcfa__isnull=False))).alias(cost_order=Case(When(kind="inventory_adjustment", then=Value(2)),
        When(quantity_milli__gt=0, then=Value(0)), default=Value(1), output_field=IntegerField())).order_by(
        "occurred_at", "cost_order", "operation__created_at", "sale_line__position", "created_at", "id")


def ordered_product_cost_events(product_id, *, until=None):
    """Merge value-only confirmations after physical movements at each UTC instant."""
    physical = causal_movement_order(ordered_product_movements(product_id, until=until).iterator(chunk_size=1000))
    adjustments = StockValuationAdjustment.objects.filter(product_id=product_id)
    if until is not None:
        adjustments = adjustments.filter(occurred_at__lte=until)
    adjustments = adjustments.select_related("product", "operation").order_by("occurred_at", "operation__created_at", "id")
    reviews = StockShortageReview.objects.filter(product_id=product_id)
    if until is not None:
        reviews = reviews.filter(occurred_at__lte=until)
    reviews = reviews.select_related("product", "operation").order_by("occurred_at", "operation__created_at", "id")
    def event_order(row):
        if row.kind in {"valuation_adjustment", "shortage_review"}:
            return row.occurred_at, 1, row.operation.created_at, str(row.id)
        return row.occurred_at, 0
    return heapq.merge(physical, adjustments.iterator(chunk_size=1000), reviews.iterator(chunk_size=1000), key=event_order)


@transaction.atomic
def freeze_physical_cost(movement):
    """Capture a new movement at its effective date, preserving all older valuations."""
    history = ordered_product_cost_events(movement.product_id, until=movement.occurred_at)
    for row, after, cost in iter_movement_valuations(history):
        if cost is not None and row.id == movement.id:
            return freeze_movement_cost(row, after, cost)
    raise ValueError("The physical movement is absent from its valuation history.")


def freeze_operation_costs(operation, product_ids, occurred_at, *, kind):
    """Capture all lines of one operation after their physical sources exist."""
    for product_id in dict.fromkeys(product_ids):
        history = ordered_product_cost_events(product_id, until=occurred_at)
        for movement, after, cost in iter_movement_valuations(history):
            if movement.operation_id == operation.id and movement.kind == kind:
                freeze_movement_cost(movement, after, cost)


def freeze_sale_costs(sale):
    """Capture each new sale line at its effective date, without revising older costs."""
    freeze_operation_costs(sale.operation, sale.lines.order_by().values_list("product_id", flat=True).distinct(),
        sale.occurred_at, kind="sale")
