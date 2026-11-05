"""Append alongside the source transaction; projections never rewrite history."""
import uuid

from customers.models import CustomerLedgerEntry


def append_customer_ledger(shop, customer, operation, source_id, kind, occurred_at,
                           delta_fcfa, debt_delta_fcfa=0, credit_delta_fcfa=0, sequence=0):
    return CustomerLedgerEntry.objects.create(
        id=uuid.uuid5(source_id, "customer-ledger:" + kind), shop=shop, customer=customer, operation=operation,
        source_id=source_id, kind=kind, sequence=sequence, occurred_at=occurred_at, recorded_at=operation.created_at,
        delta_fcfa=delta_fcfa, debt_delta_fcfa=debt_delta_fcfa, credit_delta_fcfa=credit_delta_fcfa,
    )
