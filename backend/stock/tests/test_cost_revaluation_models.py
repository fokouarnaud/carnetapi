"""US-34: a cost revision keeps both original cost records and real-world operations."""
import uuid
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from common.models import Activity
from cash.models import CashMovement
from stock.models import CostRevaluation, CostRevaluationItem, ReturnCostReconstruction, ReturnCostRevision, StockCostSnapshot, StockMovement
from identity.models import Installation
from purchasing.models import SupplierDebt
from stock.tests import test_return_costs as return_tests


class CostRevaluationModelTests(TestCase):
    setUp = return_tests.ReturnCostTests.setUp
    authenticate = return_tests.ReturnCostTests.authenticate
    url = return_tests.ReturnCostTests.url
    payload = return_tests.ReturnCostTests.payload
    invoice_and_receipts = return_tests.ReturnCostTests.invoice_and_receipts
    make_sale = return_tests.ReturnCostTests.make_sale
    event = return_tests.ReturnCostTests.event
    return_path = return_tests.ReturnCostTests.return_path
    receive = return_tests.ReturnCostTests.receive
    restock = return_tests.ReturnCostTests.restock

    def test_reconstructed_legacy_cost_identifies_its_batch_and_preserves_unknown_instead_of_zero(self):
        self.invoice_and_receipts(total_price=1000, quantity=2000, parts=(2000,))
        sale = self.make_sale(2000)
        receipt, _ = self.receive(self.return_path(sale, 1000))
        original = receipt.cost_snapshot
        original.sale_cost = None
        original.cost_state = "unknown"
        original.total_cost_nanos_fcfa = original.unit_cost_micros_numerator = original.unit_cost_micros_denominator = None
        original.save()
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="cost_revalued",
            target_id=self.product.id, reason="Reprise explicitement contrôlée")
        batch = CostRevaluation.objects.create(product=self.product, operation=event,
            installation=Installation.objects.get(user=self.owner))
        reconstruction = ReturnCostReconstruction.objects.create(original=original, batch=batch,
            previous_legacy_unit_cost_micros=None, previous_cost_is_provisional=True)
        original.refresh_from_db()
        self.assertIsNone(original.total_cost_nanos_fcfa)
        self.assertTrue(reconstruction.previous_cost_is_provisional)
        with self.assertRaises(ProtectedError):
            batch.delete()
        with self.assertRaises(ProtectedError):
            original.delete()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReturnCostReconstruction.objects.create(original=original, batch=batch,
                previous_legacy_unit_cost_micros=0, previous_cost_is_provisional=False)

    def test_late_fee_replays_sale_return_restock_and_later_sale_without_replacing_originals(self):
        invoice, _incoming = self.invoice_and_receipts(total_price=1000, quantity=2000, parts=(2000,))
        sale = self.make_sale(2000)
        path = self.return_path(sale, 1000)
        receipt, _ = self.receive(path)
        self.restock(path, receipt, 1000)
        later_sale = self.make_sale(1000)
        old_sale = StockCostSnapshot.objects.get(movement__sale_line__sale_id=sale["id"])
        old_later = StockCostSnapshot.objects.get(movement__sale_line__sale_id=later_sale["id"])
        fee = self.client.post(self.url() + invoice["id"] + "/charges/", self.event(label="Transport",
            amount_fcfa=200, allocations=[{"line_id": invoice["lines"][0]["id"], "amount_fcfa": 200}]), format="json")
        self.assertEqual(fee.status_code, 201, fee.content)
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="cost_revalued",
            target_id=self.product.id, reason="Intégration du transport tardif")
        batch = CostRevaluation.objects.create(product=self.product, operation=event,
            installation=Installation.objects.get(user=self.owner))
        from stock.services.cost_revaluation import revalue_product_costs
        revalue_product_costs(batch)
        first = StockCostSnapshot.objects.get(supersedes=old_sale)
        last = StockCostSnapshot.objects.get(supersedes=old_later)
        revised_return = ReturnCostRevision.objects.get(origin=receipt.cost_snapshot)
        self.assertEqual(first.total_cost_nanos_fcfa, 2200000000000)
        self.assertEqual(last.total_cost_nanos_fcfa, 1100000000000)
        self.assertEqual(revised_return.total_cost_nanos_fcfa, 1100000000000)
        self.assertEqual(revised_return.sale_cost_id, first.id)
        current = self.client.get(path + str(receipt.id) + "/")
        self.assertEqual(current.status_code, 200, current.content)
        self.assertEqual(current.data["cost"]["total_cost_nanos_fcfa"], "1100000000000")
        self.assertEqual(current.data["cost"]["receipt_cost_id"], str(receipt.cost_snapshot.id))
        self.assertEqual(current.data["cost"]["revision_id"], str(revised_return.id))
        self.assertEqual(current.data["cost"]["version"], 2)
        self.assertEqual(current.data["unit_cost_micros"], 1100000000)
        decisions = self.client.get(path + str(receipt.id) + "/decisions/")
        self.assertEqual(decisions.data["results"][0]["unit_cost_micros"], 1100000000)
        self.assertEqual(old_sale.total_cost_nanos_fcfa, 2000000000000)
        self.assertEqual(receipt.cost_snapshot.total_cost_nanos_fcfa, 1000000000000)
        self.assertEqual(StockMovement.objects.count(), 4)
        self.assertEqual(sum(CashMovement.objects.values_list("amount_fcfa", flat=True)), 300)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 2000)
        counts = (StockCostSnapshot.objects.count(), ReturnCostRevision.objects.count())
        revalue_product_costs(batch)
        self.assertEqual((StockCostSnapshot.objects.count(), ReturnCostRevision.objects.count()), counts)
        second_receipt, response = self.receive(self.return_path(sale, 1000), condition="sellable")
        self.assertEqual(response.data["cost"]["total_cost_nanos_fcfa"], "1100000000000")
        self.assertEqual(second_receipt.cost_snapshot.sale_cost_id, first.id)

    def test_revision_batch_protects_old_sale_and_receipt_costs_without_second_stock_cash_or_debt(self):
        self.invoice_and_receipts(total_price=1000, quantity=2000, parts=(2000,))
        sale = self.make_sale(2000)
        received, _response = self.receive(self.return_path(sale, 1000))
        original = StockCostSnapshot.objects.get(movement__sale_line__sale_id=sale["id"])
        original_return = received.cost_snapshot
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="cost_revalued", target_id=self.product.id,
            reason="Transport reçu tardivement")
        batch = CostRevaluation.objects.create(product=self.product, operation=event,
            installation=Installation.objects.get(user=self.owner), source_revision_ids=[])
        revised = StockCostSnapshot.objects.create(movement=original.movement, trigger_operation=event,
            version=2, supersedes=original, cost_state="known", total_cost_nanos_fcfa=2200000000000,
            unit_cost_micros_numerator=1100000000, unit_cost_micros_denominator=1,
            quantity_after_milli=0, stock_value_after_nanos_fcfa=0)
        change = CostRevaluationItem.objects.create(batch=batch, movement=original.movement, previous=original, revised=revised)
        revised_return = ReturnCostRevision.objects.create(origin=original_return, batch=batch, version=2,
            sale_cost=revised, cost_state="known", total_cost_nanos_fcfa=1100000000000,
            unit_cost_micros_numerator=1100000000, unit_cost_micros_denominator=1)
        original.refresh_from_db()
        original_return.refresh_from_db()
        self.assertEqual(original.total_cost_nanos_fcfa, 2000000000000)
        self.assertEqual(original_return.total_cost_nanos_fcfa, 1000000000000)
        self.assertEqual(revised_return.origin_id, original_return.id)
        self.assertEqual(change.previous_id, original.id)
        self.assertEqual(StockMovement.objects.count(), 2)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 200)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 2000)
        with self.assertRaises(ProtectedError):
            batch.delete()
        with self.assertRaises(ProtectedError):
            original_return.delete()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReturnCostRevision.objects.create(origin=original_return, batch=batch, version=3, supersedes=revised_return,
                cost_state="unknown")
        with self.assertRaises(IntegrityError), transaction.atomic():
            CostRevaluationItem.objects.create(batch=batch, movement=original.movement, previous=original, revised=revised)
