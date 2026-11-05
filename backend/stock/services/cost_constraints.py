"""Exact integer JSON guards for cost amounts exceeding SQLite's integer range."""
import json


_INVALID = object()


def _integer(value):
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray)):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return _INVALID
    return value if type(value) is int else _INVALID


def snapshot_values_valid(state, total, numerator, denominator, quantity_after, value_after):
    total, numerator, denominator, quantity_after, value_after = map(_integer,
        (total, numerator, denominator, quantity_after, value_after))
    if type(quantity_after) is not int:
        return False
    if value_after is not None and (type(value_after) is not int or value_after < 0):
        return False
    if quantity_after == 0 and value_after != 0:
        return False
    if quantity_after < 0 and value_after is not None:
        return False
    if state == "unknown":
        return total is None and numerator is None and denominator is None
    return (state in {"known", "provisional"} and type(total) is int and total >= 0
        and type(numerator) is int and numerator >= 0 and type(denominator) is int and denominator > 0
        and (numerator != 0 or total == 0))


def register_cost_functions(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        connection.connection.create_function("cost_snapshot_values_valid", 6, snapshot_values_valid, deterministic=True)
        connection.connection.create_function("cost_valuation_gap_valid", 2, valuation_gap_valid, deterministic=True)
        connection.connection.create_function("stock_valuation_values_valid", 3, stock_valuation_values_valid, deterministic=True)
        connection.connection.create_function("stock_valuation_closure_valid", 3, stock_valuation_closure_valid, deterministic=True)
        connection.connection.create_function("stock_shortage_review_valid", 2, stock_shortage_review_valid, deterministic=True)


def valuation_gap_valid(pending, gap):
    value = _integer(gap)
    return value is None or (type(value) is int and value != 0 and pending == 1)


def stock_valuation_values_valid(quantity, previous, confirmed):
    quantity, previous, confirmed = map(_integer, (quantity, previous, confirmed))
    return (type(quantity) is int and quantity > 0 and type(confirmed) is int and confirmed >= 0
        and (previous is None or (type(previous) is int and previous >= 0)))


def stock_valuation_closure_valid(quantity, previous, confirmed):
    quantity, previous, confirmed = map(_integer, (quantity, previous, confirmed))
    if type(quantity) is int and quantity == 0:
        return type(previous) is int and previous == 0 and type(confirmed) is int and confirmed == 0
    return stock_valuation_values_valid(quantity, previous, confirmed)


def stock_shortage_review_valid(quantity, value):
    quantity, value = map(_integer, (quantity, value))
    return (type(quantity) is int and quantity >= 0 and
        (value is None or (type(value) is int and value >= 0)) and (quantity != 0 or value == 0))
