"""Freeze original supplier account effects; mutable remaining balances are not inputs."""
import uuid
from itertools import groupby
from django.db import migrations


def populate(apps, schema_editor):
    database = schema_editor.connection.alias
    Ledger = apps.get_model("core", "SupplierLedgerEntry")
    def objects(name):
        return apps.get_model("core", name).objects.using(database)
    existing = set(Ledger.objects.using(database).values_list("operation_id", flat=True))
    def append(shop_id, supplier_id, event, source_id, kind, occurred_at, debt=0, advance=0, credit=0, reference=""):
        values = dict(id=uuid.uuid5(source_id, "supplier-ledger:" + kind), shop_id=shop_id, supplier_id=supplier_id,
            operation_id=event.id, source_id=source_id, kind=kind, sequence=0, occurred_at=occurred_at,
            recorded_at=event.created_at, external_reference=reference, state="validated",
            delta_fcfa=debt - advance - credit, debt_delta_fcfa=debt, advance_delta_fcfa=advance, credit_delta_fcfa=credit)
        if event.id in existing:
            row = Ledger.objects.using(database).get(operation_id=event.id, sequence=0)
            if any(getattr(row, key) != value for key, value in values.items()):
                raise RuntimeError("Inconsistent supplier ledger operation: " + str(event.id))
            return
        Ledger.objects.using(database).create(**values)
        existing.add(event.id)
    def party(credit):
        if credit.supplier_return_id is not None:
            document = credit.supplier_return.document
            return document.shop_id, document.supplier_id, document.external_reference
        return credit.opening_shop_id, credit.opening_supplier_id, ""
    for row in objects("SupplierDebt").select_related("operation", "document").iterator(chunk_size=1000):
        append(row.shop_id, row.supplier_id, row.operation, row.document_id or row.id,
            "invoice" if row.document_id else "opening_debt", row.occurred_at, debt=row.amount_fcfa,
            reference=row.document.external_reference if row.document_id else "")
    for row in objects("SupplierPayment").select_related("operation").iterator(chunk_size=1000):
        append(row.shop_id, row.supplier_id, row.operation, row.id, "payment", row.occurred_at, advance=row.amount_fcfa)
    allocations = objects("SupplierPaymentAllocation").select_related("operation", "payment").order_by("operation_id")
    for _, group in groupby(allocations.iterator(chunk_size=1000), key=lambda row: row.operation_id):
        row = next(group)
        amount = row.amount_fcfa + sum(part.amount_fcfa for part in group)
        append(row.payment.shop_id, row.payment.supplier_id, row.operation, row.operation_id, "payment_allocation",
               row.operation.created_at, debt=-amount, advance=-amount)
    for row in objects("SupplierReturn").select_related("operation", "document").iterator(chunk_size=1000):
        append(row.document.shop_id, row.document.supplier_id, row.operation, row.id, "supplier_return", row.occurred_at,
               reference=row.document.external_reference)
    for row in objects("SupplierCredit").select_related("operation", "supplier_return__document").iterator(chunk_size=1000):
        shop_id, supplier_id, reference = party(row)
        if row.kind == "opening_advance":
            append(shop_id, supplier_id, row.operation, row.id, "opening_advance", row.occurred_at, advance=row.amount_fcfa)
        elif row.kind == "opening_credit":
            append(shop_id, supplier_id, row.operation, row.id, "opening_credit", row.occurred_at, credit=row.amount_fcfa)
        else:
            append(shop_id, supplier_id, row.operation, row.id, "return_credit", row.occurred_at,
                debt=-row.applied_to_origin_fcfa, credit=row.amount_fcfa - row.applied_to_origin_fcfa, reference=reference)
    allocations = objects("SupplierCreditAllocation").select_related("operation", "credit__supplier_return__document").order_by("operation_id")
    for _, group in groupby(allocations.iterator(chunk_size=1000), key=lambda row: row.operation_id):
        row = next(group)
        amount = row.amount_fcfa + sum(part.amount_fcfa for part in group)
        shop_id, supplier_id, reference = party(row.credit)
        advance = row.credit.kind == "opening_advance"
        append(shop_id, supplier_id, row.operation, row.operation_id, "advance_allocation" if advance else "credit_allocation",
            row.operation.created_at, debt=-amount, advance=-amount if advance else 0, credit=-amount if not advance else 0)
    for row in objects("SupplierRefund").select_related("operation", "credit__supplier_return__document").iterator(chunk_size=1000):
        shop_id, supplier_id, reference = party(row.credit)
        advance = row.credit.kind == "opening_advance"
        append(shop_id, supplier_id, row.operation, row.id, "supplier_refund", row.occurred_at,
               advance=-row.amount_fcfa if advance else 0, credit=-row.amount_fcfa if not advance else 0)


class Migration(migrations.Migration):
    dependencies = [("core", "0060_remove_supplierledgerentry_supplier_ledger_exact_balance_and_more")]
    operations = [migrations.RunPython(populate, migrations.RunPython.noop)]
