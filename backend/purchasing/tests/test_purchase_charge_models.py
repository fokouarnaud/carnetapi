"""US-34: cost allocation history is distinct from invoices and real payments."""
import uuid
from django.db import IntegrityError, transaction, connection
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.utils import timezone
from common.models import Activity
from cash.models import CashMovement
from identity.models import Installation
from catalogue.models import Product
from purchasing.models import PurchaseCharge, PurchaseChargeAllocation, PurchaseChargeRevision, PurchaseDocument, SupplierDebt
from stock.models import StockMovement
from purchasing.tests import test_purchases as purchase_tests


class PurchaseChargeModelTests(TestCase):
    setUp = purchase_tests.PurchaseTests.setUp
    authenticate = purchase_tests.PurchaseTests.authenticate
    payload = purchase_tests.PurchaseTests.payload
    url = purchase_tests.PurchaseTests.url

    def invoice(self, first_price=1000):
        other = Product.objects.create(shop=self.shop, name="B", unit_kind="piece", unit_label="pièce", sale_price_fcfa=4000)
        result = self.client.post(self.url(), self.payload(lines=[
            {"id": str(uuid.uuid4()), "product_id": str(self.product.id), "quantity_milli": 10000, "unit_price_fcfa": first_price},
            {"id": str(uuid.uuid4()), "product_id": str(other.id), "quantity_milli": 10000, "unit_price_fcfa": 3000},
        ]), format="json")
        self.assertEqual(result.status_code, 201, result.content)
        return PurchaseDocument.objects.get(id=result.data["id"])

    def charge(self):
        document = self.invoice()
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="purchase_charge_recorded",
            target_id=uuid.uuid4(), reason="Transport directement affecté")
        charge = PurchaseCharge.objects.create(id=event.target_id, document=document, operation=event,
            installation=Installation.objects.get(user=self.owner), occurred_at=timezone.now(), label="Transport", amount_fcfa=4000)
        revision = PurchaseChargeRevision.objects.create(charge=charge, operation=event, version=1)
        allocations = [PurchaseChargeAllocation.objects.create(revision=revision, line=line, amount_fcfa=amount)
            for line, amount in zip(document.lines.all(), (1000, 3000), strict=True)]
        return charge, revision, allocations

    def test_charge_reallocation_keeps_original_allocations_invoice_debt_stock_and_cash(self):
        charge, first, original = self.charge()
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="purchase_charge_reallocated",
            target_id=charge.id, reason="Répartition vérifiée selon les colis")
        second = PurchaseChargeRevision.objects.create(charge=charge, operation=event, version=2, supersedes=first)
        for row, amount in zip(original, (1500, 2500), strict=True):
            PurchaseChargeAllocation.objects.create(revision=second, line=row.line, amount_fcfa=amount)
        self.assertEqual(list(first.allocations.order_by("line__position").values_list("amount_fcfa", flat=True)), [1000, 3000])
        self.assertEqual(list(second.allocations.order_by("line__position").values_list("amount_fcfa", flat=True)), [1500, 2500])
        self.assertEqual(charge.document.total_fcfa, 40000)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 40000)
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(StockMovement.objects.exists())
        with self.assertRaises(ProtectedError):
            first.delete()
        with self.assertRaises(ProtectedError):
            charge.document.delete()

    def test_database_rejects_fractional_negative_duplicate_and_broken_revision_values(self):
        charge, revision, rows = self.charge()
        for table, identity, field, value in (
            ("purchasecharge", charge.id, "amount_fcfa", -1), ("purchasecharge", charge.id, "amount_fcfa", 0.5),
            ("purchasechargerevision", revision.id, "version", 0), ("purchasechargerevision", revision.id, "version", 1.5),
            ("purchasechargerevision", revision.id, "version", 2),
            ("purchasechargeallocation", rows[0].id, "amount_fcfa", -1),
            ("purchasechargeallocation", rows[0].id, "amount_fcfa", 1.5),
        ):
            with self.subTest(table=table, field=field, value=value), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(f'UPDATE core_{table} SET "{field}" = %s WHERE id = %s', [value, identity.hex])
        with self.assertRaises(IntegrityError), transaction.atomic():
            PurchaseChargeAllocation.objects.create(revision=revision, line=rows[0].line, amount_fcfa=0)
        PurchaseChargeAllocation.objects.filter(id=rows[0].id).update(amount_fcfa=0)
        self.assertEqual(PurchaseChargeAllocation.objects.get(id=rows[0].id).amount_fcfa, 0)
