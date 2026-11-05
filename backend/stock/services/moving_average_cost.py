"""Ordered CUMP arithmetic on integer nano-FCFA values, with no I/O or clock."""
from dataclasses import dataclass, replace
from fractions import Fraction


NANOS_PER_FCFA = 1000000000


def _quantity(value):
    if type(value) is not int or value <= 0:
        raise ValueError("Quantity must be a positive integer in thousandths.")


def _rounded(numerator, denominator):
    quotient, remainder = divmod(numerator, denominator)
    return quotient + (2 * remainder >= denominator)


def receipt_cost_nanos(total_fcfa, original_quantity_milli, received_before_milli, quantity_milli):
    """Allocate the invoice cost by cumulative half-up differences, preserving its total."""
    _quantity(original_quantity_milli)
    _quantity(quantity_milli)
    if (type(total_fcfa) is not int or total_fcfa < 0 or type(received_before_milli) is not int
            or received_before_milli < 0 or received_before_milli + quantity_milli > original_quantity_milli):
        raise ValueError("Invalid invoice cost or cumulative received quantity.")
    def cumulative(quantity):
        return _rounded(total_fcfa * NANOS_PER_FCFA * quantity, original_quantity_milli)
    return cumulative(received_before_milli + quantity_milli) - cumulative(received_before_milli)


def returned_cost_nanos(original_total_nanos_fcfa, original_quantity_milli, restocked_before_milli, quantity_milli):
    """Allocate only the original sale cost to physically restocked goods."""
    _quantity(original_quantity_milli)
    _quantity(quantity_milli)
    if (type(restocked_before_milli) is not int or restocked_before_milli < 0
            or restocked_before_milli + quantity_milli > original_quantity_milli):
        raise ValueError("Restocked quantity exceeds the original sale.")
    if original_total_nanos_fcfa is None:
        return None
    if type(original_total_nanos_fcfa) is not int or original_total_nanos_fcfa < 0:
        raise ValueError("Original cost must be a nonnegative integer or unknown.")
    def cumulative(quantity):
        return _rounded(original_total_nanos_fcfa * quantity, original_quantity_milli)
    return cumulative(restocked_before_milli + quantity_milli) - cumulative(restocked_before_milli)


@dataclass(frozen=True)
class AllocatedCost:
    total_nanos_fcfa: int | None
    unit_cost_micros: Fraction | None
    state: str
    provisional_quantity_milli: int
    valuation_gap_nanos_fcfa: int | None = None


@dataclass(frozen=True)
class AverageCostState:
    quantity_milli: int = 0
    bucket_quantity_milli: int = 0
    bucket_cost_nanos_fcfa: int | None = 0
    bucket_removed_milli: int = 0
    last_known_unit_cost_micros: Fraction | None = None
    has_unresolved_shortage: bool = False
    bucket_cost_is_provisional: bool = False
    has_unresolved_valuation: bool = False

    def __post_init__(self):
        if (type(self.quantity_milli) is not int or type(self.bucket_quantity_milli) is not int
                or type(self.bucket_removed_milli) is not int or self.bucket_quantity_milli < 0
                or not 0 <= self.bucket_removed_milli <= self.bucket_quantity_milli
                or type(self.has_unresolved_shortage) is not bool or type(self.bucket_cost_is_provisional) is not bool
                or type(self.has_unresolved_valuation) is not bool):
            raise ValueError("Invalid quantity or shortage state.")
        if self.bucket_cost_nanos_fcfa is not None and (type(self.bucket_cost_nanos_fcfa) is not int or self.bucket_cost_nanos_fcfa < 0):
            raise ValueError("Cost must be a nonnegative integer or unknown.")
        if self.last_known_unit_cost_micros is not None and (type(self.last_known_unit_cost_micros) is not Fraction or self.last_known_unit_cost_micros < 0):
            raise ValueError("Last known cost must be a nonnegative exact fraction.")
        expected = self.bucket_quantity_milli - self.bucket_removed_milli
        if ((self.quantity_milli >= 0 and expected != self.quantity_milli)
                or (self.quantity_milli < 0 and (expected != 0 or not self.has_unresolved_shortage))):
            raise ValueError("Quantity does not match the receipt bucket.")
        if (self.quantity_milli > 0 and self.bucket_cost_nanos_fcfa is not None
                and self.last_known_unit_cost_micros != Fraction(self.bucket_cost_nanos_fcfa, self.bucket_quantity_milli)):
            raise ValueError("Known current cost must also be the last known cost.")

    @property
    def average_unit_cost_micros(self):
        if self.quantity_milli <= 0 or self.bucket_cost_nanos_fcfa is None:
            return None
        return Fraction(self.bucket_cost_nanos_fcfa, self.bucket_quantity_milli)

    @property
    def stock_value_nanos_fcfa(self):
        if self.quantity_milli < 0:
            return None
        if self.quantity_milli == 0:
            return 0
        if self.bucket_cost_nanos_fcfa is None:
            return None
        used = _rounded(self.bucket_cost_nanos_fcfa * self.bucket_removed_milli, self.bucket_quantity_milli)
        return self.bucket_cost_nanos_fcfa - used

    def receive(self, quantity_milli, total_cost_nanos_fcfa, *, is_provisional=False):
        _quantity(quantity_milli)
        if type(is_provisional) is not bool:
            raise ValueError("Provisional state must be a boolean.")
        if total_cost_nanos_fcfa is not None and (type(total_cost_nanos_fcfa) is not int or total_cost_nanos_fcfa < 0):
            raise ValueError("Receipt cost must be a nonnegative integer or unknown.")
        if self.quantity_milli < 0:
            covered = min(-self.quantity_milli, quantity_milli)
            quantity = self.quantity_milli + quantity_milli
            last = self.last_known_unit_cost_micros
            if total_cost_nanos_fcfa is not None:
                last = Fraction(total_cost_nanos_fcfa, quantity_milli)
            return AverageCostState(quantity, quantity_milli, total_cost_nanos_fcfa, covered, last, True, is_provisional,
                self.has_unresolved_valuation)
        quantity = self.quantity_milli + quantity_milli
        before = self.stock_value_nanos_fcfa
        value = None if before is None or total_cost_nanos_fcfa is None else before + total_cost_nanos_fcfa
        last = self.last_known_unit_cost_micros if value is None else Fraction(value, quantity)
        provisional = is_provisional or (self.quantity_milli > 0 and self.bucket_cost_is_provisional)
        return AverageCostState(quantity, quantity, value, 0, last, self.has_unresolved_shortage, provisional,
            self.has_unresolved_valuation)

    def receive_at_average(self, quantity_milli):
        """Value an observed surplus at the current known CUMP, otherwise unknown."""
        _quantity(quantity_milli)
        basis = self.average_unit_cost_micros
        total = None if basis is None else _rounded(basis.numerator * quantity_milli, basis.denominator)
        provisional = self.bucket_cost_is_provisional
        after = self.receive(quantity_milli, total, is_provisional=provisional)
        return after, AllocatedCost(total, basis, "unknown" if total is None else "provisional" if provisional else "known", 0)

    def remove_at_cost(self, quantity_milli, total_cost_nanos_fcfa):
        """Remove at an explicit original cost, retaining incompatible residuals for review."""
        _quantity(quantity_milli)
        if total_cost_nanos_fcfa is not None and (type(total_cost_nanos_fcfa) is not int or total_cost_nanos_fcfa < 0):
            raise ValueError("Original departure cost must be a nonnegative integer or unknown.")
        quantity = self.quantity_milli - quantity_milli
        before = self.stock_value_nanos_fcfa
        remaining = None if before is None or total_cost_nanos_fcfa is None else before - total_cost_nanos_fcfa
        gap = remaining if remaining is not None and ((quantity > 0 and remaining < 0) or
            (quantity == 0 and remaining != 0)) else None
        value = 0 if quantity <= 0 else None if gap is not None else remaining
        last = self.last_known_unit_cost_micros
        if quantity > 0 and value is not None:
            last = Fraction(value, quantity)
        after = AverageCostState(quantity, max(quantity, 0), value, 0, last,
            self.has_unresolved_shortage or quantity < 0, self.bucket_cost_is_provisional if quantity > 0 else False,
            self.has_unresolved_valuation or gap is not None)
        basis = None if total_cost_nanos_fcfa is None else Fraction(total_cost_nanos_fcfa, quantity_milli)
        return after, AllocatedCost(total_cost_nanos_fcfa, basis, "unknown" if basis is None else "known",
            max(-quantity, 0) - max(-self.quantity_milli, 0), gap)

    def remove(self, quantity_milli):
        _quantity(quantity_milli)
        available = min(max(self.quantity_milli, 0), quantity_milli)
        excess = quantity_milli - available
        average = self.average_unit_cost_micros
        cost = 0
        if available:
            if self.bucket_cost_nanos_fcfa is None:
                cost = None
            else:
                def cumulative(quantity):
                    return _rounded(self.bucket_cost_nanos_fcfa * quantity, self.bucket_quantity_milli)
                cost = cumulative(self.bucket_removed_milli + available) - cumulative(self.bucket_removed_milli)
        if excess:
            last = self.last_known_unit_cost_micros
            estimate = None if last is None else _rounded(last.numerator * excess, last.denominator)
            cost = None if cost is None or estimate is None else cost + estimate
        basis = None if cost is None else average if available else self.last_known_unit_cost_micros
        state = "unknown" if cost is None else "provisional" if excess or self.bucket_cost_is_provisional else "known"
        after = replace(self, quantity_milli=self.quantity_milli - quantity_milli,
            bucket_removed_milli=self.bucket_removed_milli + available,
            has_unresolved_shortage=self.has_unresolved_shortage or excess > 0)
        return after, AllocatedCost(cost, basis, state, excess)
