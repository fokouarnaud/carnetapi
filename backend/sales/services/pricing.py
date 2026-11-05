"""Exact, immutable FCFA calculations; no database, clock or network access."""
from dataclasses import dataclass
import uuid

MAX_FCFA = 9223372036854775807


@dataclass(frozen=True)
class LineAmount:
    line_id: uuid.UUID
    reference_fcfa: int
    own_discount_fcfa: int = 0


@dataclass(frozen=True)
class PricedLine:
    line_id: uuid.UUID
    reference_fcfa: int
    own_discount_fcfa: int
    global_discount_fcfa: int
    net_fcfa: int


@dataclass(frozen=True)
class PricedBasket:
    lines: tuple[PricedLine, ...]
    reference_total_fcfa: int
    discount_total_fcfa: int
    net_total_fcfa: int


@dataclass(frozen=True)
class PricingFailure:
    code: str
    line_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class ReferenceAmount:
    amount_fcfa: int


def is_amount(value):
    return type(value) is int and 0 <= value <= MAX_FCFA


def price_basket(
    lines: list[LineAmount] | tuple[LineAmount, ...],
    global_discount_fcfa: int = 0,
    seller_cap_basis_points: int | None = 0,
) -> PricedBasket | PricingFailure:
    """Allocate by largest remainder, breaking ties by stable line UUID.

    One basis point is 0.01%. None bypasses only the cap calculation; the
    future API must reserve that choice to an authorized manager. Allocation
    that exceeds any line's cap is refused, never silently redistributed.
    """
    if not isinstance(lines, (list, tuple)) or not lines:
        return PricingFailure("empty_basket")
    lines = tuple(lines)
    if not is_amount(global_discount_fcfa):
        return PricingFailure("invalid_global_discount")
    if seller_cap_basis_points is not None and (
        type(seller_cap_basis_points) is not int or not 0 <= seller_cap_basis_points <= 10000
    ):
        return PricingFailure("invalid_discount_cap")
    seen = set()
    for line in lines:
        if not isinstance(line, LineAmount) or not isinstance(line.line_id, uuid.UUID):
            return PricingFailure("invalid_line_id")
        if line.line_id in seen:
            return PricingFailure("duplicate_line_id", (line.line_id,))
        seen.add(line.line_id)
        if not is_amount(line.reference_fcfa) or not is_amount(line.own_discount_fcfa):
            return PricingFailure("invalid_line_amount", (line.line_id,))
        if line.own_discount_fcfa > line.reference_fcfa:
            return PricingFailure("discount_exceeds_line", (line.line_id,))
    reference_total = sum(line.reference_fcfa for line in lines)
    if reference_total > MAX_FCFA:
        return PricingFailure("amount_out_of_range")
    weights = [line.reference_fcfa - line.own_discount_fcfa for line in lines]
    weight_total = sum(weights)
    if global_discount_fcfa > weight_total:
        return PricingFailure("discount_exceeds_total")
    shares = [0] * len(lines)
    if global_discount_fcfa:
        divisions = [divmod(global_discount_fcfa * weight, weight_total) for weight in weights]
        shares = [quotient for quotient, _ in divisions]
        remainder = global_discount_fcfa - sum(shares)
        ranked = sorted(range(len(lines)), key=lambda index: (-divisions[index][1], lines[index].line_id.int))
        for index in ranked[:remainder]:
            shares[index] += 1
    priced = tuple(
        PricedLine(line.line_id, line.reference_fcfa, line.own_discount_fcfa, share,
                   line.reference_fcfa - line.own_discount_fcfa - share)
        for line, share in zip(lines, shares, strict=True)
    )
    if seller_cap_basis_points is not None:
        violations = tuple(sorted(
            (line.line_id for line in priced
             if (line.own_discount_fcfa + line.global_discount_fcfa) * 10000
             > line.reference_fcfa * seller_cap_basis_points), key=lambda value: value.int,
        ))
        if violations:
            return PricingFailure("discount_cap_exceeded", violations)
    own_total = sum(line.own_discount_fcfa for line in lines)
    discount_total = own_total + global_discount_fcfa
    return PricedBasket(priced, reference_total, discount_total, reference_total - discount_total)


def line_reference_amount(quantity_milli: int, unit_price_fcfa: int, unit_kind: str) -> ReferenceAmount | PricingFailure:
    """Round the full line once, half up, before applying any discount."""
    if type(quantity_milli) is not int or not 0 < quantity_milli <= MAX_FCFA:
        return PricingFailure("invalid_quantity")
    if not is_amount(unit_price_fcfa):
        return PricingFailure("invalid_unit_price")
    if unit_kind not in ("piece", "weight", "volume"):
        return PricingFailure("invalid_unit_kind")
    if unit_kind == "piece" and quantity_milli % 1000:
        return PricingFailure("fractional_piece")
    amount = (quantity_milli * unit_price_fcfa + 500) // 1000
    if amount > MAX_FCFA:
        return PricingFailure("amount_out_of_range")
    return ReferenceAmount(amount)
