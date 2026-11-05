"""Value partial returns from the immutable billed net, using exact integers."""


def return_amount_fcfa(net_fcfa, original_quantity_milli, returned_before_milli, quantity_milli):
    """Return the change in cumulative half-up value; reject impossible quantities."""
    values = (net_fcfa, original_quantity_milli, returned_before_milli, quantity_milli)
    if any(type(value) is not int for value in values):
        raise ValueError("Return quantities and money must be integers.")
    after = returned_before_milli + quantity_milli
    if (net_fcfa < 0 or original_quantity_milli <= 0 or returned_before_milli < 0
            or quantity_milli <= 0 or after > original_quantity_milli):
        raise ValueError("Return exceeds the invoiced quantity or has invalid values.")

    def cumulative(quantity):
        quotient, remainder = divmod(net_fcfa * quantity, original_quantity_milli)
        return quotient + (2 * remainder >= original_quantity_milli)

    return cumulative(after) - cumulative(returned_before_milli)
