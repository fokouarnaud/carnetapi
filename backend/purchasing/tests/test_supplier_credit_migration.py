"""A pre-opening supplier credit and its refund survive the nullable-origin upgrade."""
import uuid
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class SupplierCreditMigrationTests(TransactionTestCase):
    def test_existing_return_claim_keeps_its_uuid_money_origin_receipt_and_refund(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        previous = [("core", "0057_catalogueimport_kind_and_more")]
        try:
            executor.migrate(previous)
            old = executor.loader.project_state(previous).apps
            def model(name):
                return old.get_model("core", name)
            owner = model("User").objects.create(username="old-credit")
            shop = model("Shop").objects.create(owner=owner, name="Avant ouverture")
            installation = model("Installation").objects.create(user=owner)
            supplier = model("Supplier").objects.create(shop=shop, name="Fournisseur")
            at = timezone.now()
            def event(action):
                return model("Activity").objects.create(shop=shop, actor=owner, action=action,
                    target_id=uuid.uuid4(), reason="Historique conservé")
            invoice_event = event("supplier_invoice_created")
            invoice = model("PurchaseDocument").objects.create(shop=shop, supplier=supplier,
                operation=invoice_event, installation=installation, kind="invoice", occurred_at=at, total_fcfa=10000)
            departure = model("SupplierReturn").objects.create(document=invoice, operation=event("supplier_return_recorded"),
                installation=installation, occurred_at=at)
            credit_event = event("supplier_credit_accepted")
            credit = model("SupplierCredit").objects.create(supplier_return=departure, operation=credit_event,
                installation=installation, occurred_at=at, reference_fcfa=10000, amount_fcfa=5000,
                applied_to_origin_fcfa=3000, remaining_fcfa=1500)
            receipt = {"id": str(credit.id), "amount_fcfa": 5000, "remaining_fcfa": 2000}
            credit_event.details = {"response": receipt}
            credit_event.save(update_fields=["details"])
            refund = model("SupplierRefund").objects.create(credit=credit, operation=event("supplier_refund_received"),
                installation=installation, occurred_at=at, method="orange", amount_fcfa=500,
                after_remaining_fcfa=1500)
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            current = executor.loader.project_state(latest).apps
            migrated = current.get_model("purchasing", "SupplierCredit").objects.get(id=credit.id)
            self.assertEqual(migrated.kind, "return_credit")
            self.assertEqual(migrated.supplier_return_id, departure.id)
            self.assertIsNone(migrated.opening_shop_id)
            self.assertIsNone(migrated.opening_supplier_id)
            self.assertEqual((migrated.reference_fcfa, migrated.amount_fcfa, migrated.applied_to_origin_fcfa, migrated.remaining_fcfa),
                             (10000, 5000, 3000, 1500))
            self.assertEqual(current.get_model("purchasing", "SupplierRefund").objects.get(id=refund.id).credit_id, credit.id)
            self.assertEqual(current.get_model("core", "Activity").objects.get(id=credit_event.id).details["response"], receipt)
            self.assertEqual(current.get_model("purchasing", "SupplierCredit").objects.count(), 1)
        finally:
            MigrationExecutor(connection).migrate(latest)
