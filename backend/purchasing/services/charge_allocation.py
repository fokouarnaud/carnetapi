"""Exact direct-cost allocation by invoice value, with stable largest remainders."""
from dataclasses import dataclass
from uuid import UUID
from sales.services.pricing import MAX_FCFA


@dataclass(frozen=True)
class ChargeWeight:
    line_id: UUID
    net_value_fcfa: int | None


@dataclass(frozen=True)
class ChargeAllocation:
    line_id: UUID
    amount_fcfa: int


@dataclass(frozen=True)
class ChargeProposal:
    allocations: tuple[ChargeAllocation, ...]


@dataclass(frozen=True)
class ChargeAllocationFailure:
    code: str
    line_ids: tuple[UUID, ...] = ()


def allocate_charge(amount_fcfa, lines):
    if type(amount_fcfa) is not int or not 0 <= amount_fcfa <= MAX_FCFA:
        return ChargeAllocationFailure("invalid_charge_amount")
    lines = tuple(lines)
    if not lines:
        return ChargeAllocationFailure("charge_lines_required")
    ids = [line.line_id for line in lines]
    if any(not isinstance(identity, UUID) for identity in ids):
        return ChargeAllocationFailure("invalid_charge_line")
    if len(set(ids)) != len(ids):
        return ChargeAllocationFailure("duplicate_charge_line")
    invalid = tuple(line.line_id for line in lines if line.net_value_fcfa is not None
        and (type(line.net_value_fcfa) is not int or not 0 <= line.net_value_fcfa <= MAX_FCFA))
    if invalid:
        return ChargeAllocationFailure("invalid_charge_value", invalid)
    explicit = tuple(line.line_id for line in lines if line.net_value_fcfa in (0, None))
    if explicit:
        return ChargeAllocationFailure("explicit_charge_allocation_required", explicit)
    weight = sum(line.net_value_fcfa for line in lines)
    divisions = [divmod(amount_fcfa * line.net_value_fcfa, weight) for line in lines]
    shares = [quotient for quotient, _remainder in divisions]
    remaining = amount_fcfa - sum(shares)
    ranked = sorted(range(len(lines)), key=lambda index: (-divisions[index][1], lines[index].line_id.int))
    for index in ranked[:remaining]:
        shares[index] += 1
    return ChargeProposal(tuple(ChargeAllocation(line.line_id, value) for line, value in zip(lines, shares, strict=True)))
