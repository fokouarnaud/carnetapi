"""US-34: manager previews and versioned fee allocations never create fictitious payments."""
import uuid
from django.test import TestCase
from django.utils import timezone
from cash.models import CashMovement
from identity.models import Membership
from purchasing.models import PurchaseCharge, SupplierDebt
from stock.models import StockCostSnapshot, StockMovement
from purchasing.tests import test_purchase_charge_models as model_tests


class PurchaseChargeTests(TestCase):
    setUp = model_tests.PurchaseChargeModelTests.setUp
    authenticate = model_tests.PurchaseChargeModelTests.authenticate
    payload = model_tests.PurchaseChargeModelTests.payload
    url = model_tests.PurchaseChargeModelTests.url
    invoice = model_tests.PurchaseChargeModelTests.invoice

    def path(self, document):
        return self.url() + str(document.id) + "/charges/"

    def plan(self, document, amounts=(1000, 3000)):
        return [{"line_id": str(line.id), "amount_fcfa": value}
            for line, value in zip(document.lines.all(), amounts, strict=True)]

    def charge_payload(self, document, **changes):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
            "reason": "Transport confirmé", "label": "Transport", "amount_fcfa": 4000,
            "allocations": self.plan(document), **changes}

    def test_preview_proposes_1000_3000_without_any_fee_or_financial_write(self):
        document = self.invoice()
        response = self.client.post(self.path(document) + "preview/", {"amount_fcfa": 4000}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["allocations"], self.plan(document))
        self.assertFalse(PurchaseCharge.objects.exists())
        self.assertFalse(CashMovement.objects.exists())
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 40000)

    def test_confirmation_and_reallocation_preserve_original_receipts_plans_and_actual_expense(self):
        document = self.invoice()
        expense = self.client.post(f"/api/v1/shops/{self.shop.id}/cash/", {"id": str(uuid.uuid4()),
            "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(), "reason": "Transport payé",
            "kind": "expense", "method": "cash", "amount_fcfa": 4000}, format="json")
        self.assertEqual(expense.status_code, 201)
        body = self.charge_payload(document)
        created = self.client.post(self.path(document), body, format="json")
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.data["current_revision"]["allocations"], self.plan(document))
        revision_path = self.path(document) + created.data["id"] + "/revisions/"
        correction = {"operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Poids réel des colis",
            "allocations": self.plan(document, (1500, 2500))}
        changed = self.client.post(revision_path, correction, format="json")
        self.assertEqual(changed.status_code, 201, changed.content)
        self.assertEqual(changed.data["version"], 2)
        self.assertEqual(self.client.post(revision_path, correction, format="json").data, changed.data)
        self.assertEqual(self.client.post(self.path(document), body, format="json").data, created.data)
        history = self.client.get(revision_path, {"page_size": 1})
        self.assertEqual(history.data["count"], 2)
        self.assertEqual(history.data["results"][0]["allocations"], self.plan(document))
        current = self.client.get(self.path(document) + created.data["id"] + "/")
        self.assertEqual(current.data["current_revision"]["version"], 2)
        stale = {**correction, "operation_id": str(uuid.uuid4())}
        self.assertEqual(self.client.post(revision_path, stale, format="json").status_code, 409)
        self.assertEqual(CashMovement.objects.count(), 1)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, -4000)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 40000)
        self.assertFalse(StockMovement.objects.exists())

    def test_total_mismatch_duplicate_foreign_line_and_changed_retry_are_atomic(self):
        document = self.invoice()
        bad = self.charge_payload(document, allocations=self.plan(document, (1000, 2000)))
        self.assertEqual(self.client.post(self.path(document), bad, format="json").status_code, 400)
        duplicate = self.charge_payload(document)
        duplicate["allocations"] = [duplicate["allocations"][0], duplicate["allocations"][0]]
        self.assertEqual(self.client.post(self.path(document), duplicate, format="json").status_code, 400)
        other = self.invoice()
        self.assertEqual(self.client.post(self.path(document), self.charge_payload(document,
            allocations=self.plan(other)), format="json").status_code, 404)
        self.assertFalse(PurchaseCharge.objects.exists())
        valid = self.charge_payload(document)
        self.assertEqual(self.client.post(self.path(document), valid, format="json").status_code, 201)
        self.assertEqual(self.client.post(self.path(document), {**valid, "label": "Autre"}, format="json").status_code, 409)
        self.assertEqual(PurchaseCharge.objects.count(), 1)

    def test_free_invoice_line_requires_manual_plan_and_a_seller_cannot_manage_costs(self):
        document = self.invoice(first_price=0)
        response = self.client.post(self.path(document) + "preview/", {"amount_fcfa": 4000}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "explicit_charge_allocation_required")
        manual = self.charge_payload(document, allocations=self.plan(document, (2000, 2000)))
        self.assertEqual(self.client.post(self.path(document), manual, format="json").status_code, 201)
        self.authenticate(self.seller)
        Membership.objects.filter(user=self.seller).update(can_read_cost=True)
        self.assertEqual(self.client.get(self.path(document)).status_code, 403)
        self.assertEqual(self.client.post(self.path(document) + "preview/", {"amount_fcfa": 4000}, format="json").status_code, 403)
        self.assertEqual(self.client.post(self.path(document), manual, format="json").status_code, 403)

    def test_new_sales_include_latest_allocated_fee_and_old_cost_is_preserved_pending_review(self):
        document = self.invoice()
        charge = self.client.post(self.path(document), self.charge_payload(document), format="json")
        self.assertEqual(charge.status_code, 201, charge.content)
        self.assertFalse(charge.data["current_revision"]["historical_costs_pending_review"])
        first_line = document.lines.first()
        received = self.client.post(self.url() + str(document.id) + "/receipts/", {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
            "reason": "Dix unités physiquement reçues", "lines": [{"line_id": str(first_line.id), "quantity_milli": 10000}]}, format="json")
        self.assertEqual(received.status_code, 201)
        def sell():
            response = self.client.post(f"/api/v1/shops/{self.shop.id}/sales/", {
                "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1, "quantity_milli": 1000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 100}]}, format="json")
            self.assertEqual(response.status_code, 201, response.content)
            return StockCostSnapshot.objects.get(movement__sale_line__sale_id=response.data["id"])
        old_cost = sell()
        self.assertEqual(old_cost.total_cost_nanos_fcfa, 1100000000000)
        path = self.path(document) + charge.data["id"] + "/revisions/"
        corrected = self.client.post(path, {"operation_id": str(uuid.uuid4()), "base_version": 1,
            "reason": "Frais réaffectés selon les colis", "allocations": self.plan(document, (2000, 2000))}, format="json")
        self.assertEqual(corrected.status_code, 201, corrected.content)
        self.assertTrue(corrected.data["historical_costs_pending_review"])
        new_cost = sell()
        self.assertEqual(new_cost.total_cost_nanos_fcfa, 1200000000000)
        old_cost.refresh_from_db()
        self.assertEqual(old_cost.total_cost_nanos_fcfa, 1100000000000)
        self.assertEqual(StockCostSnapshot.objects.filter(movement=old_cost.movement).count(), 1)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 40000)
        self.assertEqual(sum(CashMovement.objects.values_list("amount_fcfa", flat=True)), 200)

    def test_approved_partial_receipts_incorporate_600_then_400_of_a_1000_line_charge(self):
        from stock.services.movement_costs import value_ordered_movements
        document = self.invoice()
        confirmed = self.client.post(self.path(document), self.charge_payload(document), format="json")
        self.assertEqual(confirmed.status_code, 201)
        first_line = document.lines.first()
        for quantity in (6000, 4000):
            response = self.client.post(self.url() + str(document.id) + "/receipts/", {
                "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
                "reason": "Réception partielle confirmée", "lines": [{"line_id": str(first_line.id), "quantity_milli": quantity}]}, format="json")
            self.assertEqual(response.status_code, 201, response.content)
            if quantity == 6000:
                state, values = value_ordered_movements(StockMovement.objects.filter(product=self.product).order_by("occurred_at", "id"))
                self.assertEqual(values[0][2].total_nanos_fcfa, 6600000000000)
                self.assertEqual(state.quantity_milli, 6000)
        state, values = value_ordered_movements(StockMovement.objects.filter(product=self.product).order_by("occurred_at", "id"))
        self.assertEqual([cost.total_nanos_fcfa for _movement, _after, cost in values], [6600000000000, 4400000000000])
        self.assertEqual(state.stock_value_nanos_fcfa, 11000000000000)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 40000)
        self.assertFalse(CashMovement.objects.exists())
