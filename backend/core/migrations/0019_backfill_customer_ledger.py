"""Freeze reconstruction from immutable financial sources, not current balances."""
import uuid

from django.db import migrations


def backfill(apps, schema_editor):
    alias = schema_editor.connection.alias
    Ledger = apps.get_model("core", "CustomerLedgerEntry")
    Debt = apps.get_model("core", "CustomerDebt")
    Sale = apps.get_model("core", "Sale")
    Payment = apps.get_model("core", "CustomerPayment")
    Allocation = apps.get_model("core", "DebtAllocation")
    Decision = apps.get_model("core", "PaymentRegularization")
    Activity = apps.get_model("core", "Activity")

    def append(operation, source, kind, shop_id, customer_id, at, delta, debt_delta=0, credit_delta=0, sequence=0):
        values = {"operation_id": operation.id, "source_id": source.id, "kind": kind,
                  "shop_id": shop_id, "customer_id": customer_id, "occurred_at": at,
                  "recorded_at": operation.created_at, "delta_fcfa": delta,
                  "debt_delta_fcfa": debt_delta, "credit_delta_fcfa": credit_delta, "sequence": sequence}
        entry, created = Ledger.objects.using(alias).get_or_create(
            id=uuid.uuid5(source.id, "customer-ledger:" + kind), defaults=values,
        )
        if not created and any(getattr(entry, name) != value for name, value in values.items()):
            raise ValueError("Existing ledger entry differs from its immutable financial source.")

    for debt in Debt.objects.using(alias).filter(sale__isnull=True).select_related("operation").iterator():
        append(debt.operation, debt, "opening_debt", debt.shop_id, debt.customer_id,
               debt.occurred_at, debt.amount_fcfa, debt_delta=debt.amount_fcfa)
    for sale in Sale.objects.using(alias).filter(customer__isnull=False).select_related("operation").iterator():
        parts = list(sale.payments.using(alias).order_by("position", "id"))
        paid = sum(part.amount_fcfa for part in parts)
        append(sale.operation, sale, "sale", sale.shop_id, sale.customer_id, sale.occurred_at,
               sale.net_total_fcfa, debt_delta=sale.net_total_fcfa - paid)
        for position, part in enumerate(parts):
            append(sale.operation, part, "sale_payment", sale.shop_id, sale.customer_id,
                   sale.occurred_at, -part.amount_fcfa, sequence=position + 1)
    for payment in Payment.objects.using(alias).select_related("operation").iterator():
        applied = sum(Allocation.objects.using(alias).filter(payment=payment).values_list("applied_amount_fcfa", flat=True))
        append(payment.operation, payment, "collection", payment.shop_id, payment.customer_id,
               payment.occurred_at, -payment.amount_fcfa, debt_delta=-applied, credit_delta=payment.amount_fcfa - applied)
    for decision in Decision.objects.using(alias).select_related("operation", "payment").iterator():
        append(decision.operation, decision, decision.kind, decision.payment.shop_id, decision.payment.customer_id,
               decision.occurred_at, decision.amount_fcfa if decision.kind == "refund" else 0,
               debt_delta=-decision.amount_fcfa if decision.kind == "allocation" else 0,
               credit_delta=-decision.amount_fcfa)
    for event in Activity.objects.using(alias).filter(action="customer_debt_due_changed").iterator():
        debt = Debt.objects.using(alias).get(id=event.target_id, shop_id=event.shop_id)
        append(event, event, "due_date_changed", debt.shop_id, debt.customer_id, event.created_at, 0)


class Migration(migrations.Migration):
    dependencies = [("core", "0018_customerledgerentry")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
