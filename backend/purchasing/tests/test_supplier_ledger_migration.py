"""Existing supplier funds must be replayed from original facts, not mutable remainders."""
import uuid
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class SupplierLedgerMigrationTests(TransactionTestCase):
    def test_openings_allocations_and_refund_recover_original_effects_once(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        before = [("core", "0058_remove_suppliercredit_supplier_credit_integer_amounts_and_more")]
        try:
            executor.migrate(before)
            apps = executor.loader.project_state(before).apps
            def model(name):
                return apps.get_model("core", name)
            owner = model("User").objects.create(username="supplier-ledger-before")
            shop = model("Shop").objects.create(owner=owner, name="Historique")
            supplier = model("Supplier").objects.create(shop=shop, name="Fournisseur")
            device = model("Installation").objects.create(user=owner)
            at = timezone.now()
            def event(action):
                return model("Activity").objects.create(shop=shop, actor=owner, action=action, target_id=uuid.uuid4(), reason="Historique")
            debt_event = event("supplier_debt_opened")
            debt = model("SupplierDebt").objects.create(shop=shop, supplier=supplier, operation=debt_event, installation=device,
                occurred_at=at, amount_fcfa=10000, remaining_fcfa=8000)
            advance = model("SupplierCredit").objects.create(kind="opening_advance", opening_shop=shop, opening_supplier=supplier,
                operation=event("supplier_balance_opened"), installation=device, occurred_at=at, reference_fcfa=None,
                amount_fcfa=3000, applied_to_origin_fcfa=0, remaining_fcfa=1000)
            credit = model("SupplierCredit").objects.create(kind="opening_credit", opening_shop=shop, opening_supplier=supplier,
                operation=event("supplier_balance_opened"), installation=device, occurred_at=at, reference_fcfa=None,
                amount_fcfa=2000, applied_to_origin_fcfa=0, remaining_fcfa=1500)
            decision = event("supplier_credit_allocated")
            model("SupplierCreditAllocation").objects.create(credit=advance, debt=debt, operation=decision, position=0, amount_fcfa=2000)
            model("SupplierRefund").objects.create(credit=credit, operation=event("supplier_refund_received"), installation=device,
                occurred_at=at, method="orange", amount_fcfa=500, after_remaining_fcfa=1500)
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            current = executor.loader.project_state(latest).apps
            Ledger = current.get_model("purchasing", "SupplierLedgerEntry")
            rows = list(Ledger.objects.filter(supplier_id=supplier.id))
            self.assertEqual(len(rows), 5)
            by_kind = {row.kind: row for row in rows}
            self.assertEqual(by_kind["opening_advance"].advance_delta_fcfa, 3000)
            self.assertEqual(by_kind["opening_credit"].credit_delta_fcfa, 2000)
            self.assertEqual(by_kind["advance_allocation"].delta_fcfa, 0)
            self.assertEqual(sum(row.delta_fcfa for row in rows), 5500)
            self.assertEqual(sum(row.debt_delta_fcfa for row in rows), 8000)
            self.assertEqual(sum(row.advance_delta_fcfa for row in rows), 1000)
            self.assertEqual(sum(row.credit_delta_fcfa for row in rows), 1500)
            self.assertEqual(by_kind["opening_debt"].id, uuid.uuid5(debt.id, "supplier-ledger:opening_debt"))
            self.assertEqual(by_kind["opening_debt"].recorded_at, debt_event.created_at)
            import importlib
            migration = importlib.import_module("core.migrations.0061_populate_supplier_ledger")
            # Re-run the historical operation with its original app identities.
            legacy = executor.loader.project_state([("core", "0080_alter_membership_options_alter_shop_options_and_more")]).apps
            with connection.schema_editor() as editor:
                migration.populate(legacy, editor)
            self.assertEqual(Ledger.objects.filter(supplier_id=supplier.id).count(), 5)
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_existing_invoice_payment_return_credit_and_refund_are_not_netted_twice(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        before = [("core", "0058_remove_suppliercredit_supplier_credit_integer_amounts_and_more")]
        try:
            executor.migrate(before)
            apps = executor.loader.project_state(before).apps
            def model(name):
                return apps.get_model("core", name)
            owner = model("User").objects.create(username="supplier-ledger-transactions")
            shop = model("Shop").objects.create(owner=owner, name="Historique commercial")
            supplier = model("Supplier").objects.create(shop=shop, name="Fournisseur")
            device = model("Installation").objects.create(user=owner)
            at = timezone.now()
            def event(action):
                return model("Activity").objects.create(shop=shop, actor=owner, action=action, target_id=uuid.uuid4(), reason="Historique")
            invoice_event = event("purchase_invoice_created")
            invoice = model("PurchaseDocument").objects.create(shop=shop, supplier=supplier, operation=invoice_event,
                installation=device, kind="invoice", occurred_at=at, total_fcfa=10000, external_reference="FOURN-001")
            debt = model("SupplierDebt").objects.create(shop=shop, supplier=supplier, document=invoice, operation=invoice_event,
                installation=device, occurred_at=at, amount_fcfa=10000, remaining_fcfa=0)
            payment = model("SupplierPayment").objects.create(shop=shop, supplier=supplier, operation=event("supplier_payment_created"),
                installation=device, occurred_at=at, amount_fcfa=3000, unallocated_fcfa=1000)
            model("SupplierPaymentAllocation").objects.create(payment=payment, debt=debt, operation=event("supplier_payment_allocated"),
                position=0, amount_fcfa=2000)
            departure = model("SupplierReturn").objects.create(document=invoice, operation=event("supplier_return_departed"),
                installation=device, occurred_at=at)
            credit = model("SupplierCredit").objects.create(supplier_return=departure, operation=event("supplier_credit_accepted"),
                installation=device, occurred_at=at, reference_fcfa=5000, amount_fcfa=12000,
                applied_to_origin_fcfa=8000, remaining_fcfa=3500)
            model("SupplierRefund").objects.create(credit=credit, operation=event("supplier_refund_received"), installation=device,
                occurred_at=at, method="orange", amount_fcfa=500, after_remaining_fcfa=3500)
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            current = executor.loader.project_state(latest).apps
            rows = list(current.get_model("purchasing", "SupplierLedgerEntry").objects.filter(supplier_id=supplier.id))
            self.assertEqual(len(rows), 6)
            self.assertEqual(sum(row.delta_fcfa for row in rows), -4500)
            self.assertEqual(sum(row.debt_delta_fcfa for row in rows), 0)
            self.assertEqual(sum(row.advance_delta_fcfa for row in rows), 1000)
            self.assertEqual(sum(row.credit_delta_fcfa for row in rows), 3500)
            invoice_row = next(row for row in rows if row.kind == "invoice")
            self.assertEqual(invoice_row.source_id, invoice.id)
            self.assertEqual(invoice_row.external_reference, "FOURN-001")
        finally:
            MigrationExecutor(connection).migrate(latest)
