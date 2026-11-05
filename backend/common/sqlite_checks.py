"""Pure checks also usable when verifying a backup without Django running."""


def integer_balance_matches(delta, debt, advance, credit):
    return all(type(value) is int for value in (delta, debt, advance, credit)) and delta == debt - advance - credit
