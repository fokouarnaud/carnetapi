"""Append supplier account effects inside the business source transaction."""
import uuid
from purchasing.models import SupplierLedgerEntry
from common.sqlite_checks import integer_balance_matches


def append_supplier_ledger(shop, supplier, operation, source_id, kind, occurred_at,
                           debt_delta=0, advance_delta=0, credit_delta=0, external_reference=""):
    return SupplierLedgerEntry.objects.create(id=uuid.uuid5(source_id, "supplier-ledger:" + kind),
        shop=shop, supplier=supplier, operation=operation, source_id=source_id, kind=kind,
        occurred_at=occurred_at, recorded_at=operation.created_at, external_reference=external_reference,
        delta_fcfa=debt_delta - advance_delta - credit_delta,
        debt_delta_fcfa=debt_delta, advance_delta_fcfa=advance_delta, credit_delta_fcfa=credit_delta)


def register_supplier_ledger_functions(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        connection.connection.create_function("integer_balance_matches", 4, integer_balance_matches, deterministic=True)
