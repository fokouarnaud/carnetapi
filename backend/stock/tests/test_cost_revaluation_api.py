"""US-34: a manager approves a current preview and can inspect preserved costs."""
import uuid
from django.test import TestCase
from django.utils import timezone
from common.models import Activity
from cash.models import CashMovement
from stock.models import CostRevaluation, ReturnCostReconstruction, ReturnCostRevision, ReturnCostSnapshot, ReturnReceipt, StockCostSnapshot, StockMovement
from identity.models import DeviceSession, Membership, Shop
from catalogue.models import Product
from purchasing.models import PurchaseCharge, SupplierDebt
from sales.models import Sale, SaleLine, SaleReturnLine
from stock.tests import test_return_costs as fixtures


class CostRevaluationAPITests(TestCase):
    setUp = fixtures.ReturnCostTests.setUp
    authenticate = fixtures.ReturnCostTests.authenticate
    url = fixtures.ReturnCostTests.url
    payload = fixtures.ReturnCostTests.payload
    invoice_and_receipts = fixtures.ReturnCostTests.invoice_and_receipts
    make_sale = fixtures.ReturnCostTests.make_sale
    event = fixtures.ReturnCostTests.event
    return_path = fixtures.ReturnCostTests.return_path
    receive = fixtures.ReturnCostTests.receive

    def path(self):
        return f"/api/v1/shops/{self.shop.id}/products/{self.product.id}/cost-revaluations/"

    def scenario(self):
        invoice, _ = self.invoice_and_receipts(total_price=1000, quantity=2000, parts=(2000,))
        sale = self.make_sale(2000)
        receipt, _ = self.receive(self.return_path(sale, 1000))
        fee = self.client.post(self.url() + invoice["id"] + "/charges/", self.event(label="Transport",
            amount_fcfa=200, allocations=[{"line_id": invoice["lines"][0]["id"], "amount_fcfa": 200}]), format="json")
        self.assertEqual(fee.status_code, 201, fee.content)
        return invoice, sale, receipt

    def confirmation(self, preview):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "reason": "Frais tardifs contrôlés", "preview_token": preview["preview_token"]}

    def test_preview_has_no_writes_and_confirmation_exposes_paged_before_after_and_stable_retry(self):
        _, sale, receipt = self.scenario()
        old = StockCostSnapshot.objects.get(movement__sale_line__sale_id=sale["id"])
        counts = (Activity.objects.count(), StockCostSnapshot.objects.count())
        preview = self.client.get(self.path() + "preview/")
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual(preview.data["sales_cost_before"]["total_nanos_fcfa"], "2000000000000")
        self.assertEqual(preview.data["sales_cost_after"]["total_nanos_fcfa"], "2200000000000")
        self.assertEqual(preview.data["movement_changes"], 2)
        self.assertEqual(preview.data["return_changes"], 1)
        self.assertFalse(CostRevaluation.objects.exists())
        self.assertEqual((Activity.objects.count(), StockCostSnapshot.objects.count()), counts)
        body = self.confirmation(preview.data)
        applied = self.client.post(self.path(), body, format="json")
        self.assertEqual(applied.status_code, 201, applied.content)
        self.assertEqual(self.client.post(self.path(), body, format="json").data, applied.data)
        self.assertEqual(self.client.post(self.path(), {**body, "reason": "Autre"}, format="json").status_code, 409)
        detail_path = self.path() + applied.data["id"] + "/"
        self.assertEqual(self.client.get(detail_path).data, applied.data)
        page = self.client.get(detail_path + "items/", {"page_size": 1})
        self.assertEqual(page.data["count"], 2)
        self.assertEqual(len(page.data["results"]), 1)
        second = self.client.get(page.data["next"])
        rows = page.data["results"] + second.data["results"]
        sold = next(row for row in rows if row["movement_id"] == str(old.movement_id))
        self.assertEqual(sold["previous"]["total_cost_nanos_fcfa"], "2000000000000")
        self.assertEqual(sold["revised"]["total_cost_nanos_fcfa"], "2200000000000")
        returns = self.client.get(detail_path + "returns/")
        self.assertEqual(returns.data["count"], 1)
        self.assertEqual(returns.data["results"][0]["receipt_id"], str(receipt.id))
        self.assertEqual(returns.data["results"][0]["previous"]["total_cost_nanos_fcfa"], "1000000000000")
        self.assertEqual(returns.data["results"][0]["revised"]["total_cost_nanos_fcfa"], "1100000000000")
        self.assertEqual(StockMovement.objects.count(), 2)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 200)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 2000)

    def test_new_movement_invalidates_preview_and_confirmation_is_atomic(self):
        self.scenario()
        preview = self.client.get(self.path() + "preview/")
        self.assertEqual(preview.status_code, 200, preview.content)
        self.make_sale(1000)
        response = self.client.post(self.path(), self.confirmation(preview.data), format="json")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.data["code"], "cost_revaluation_preview_changed")
        self.assertFalse(CostRevaluation.objects.exists())
        self.assertFalse(ReturnCostRevision.objects.exists())

    def test_preview_propagates_virtual_return_cost_to_restock_and_later_sale_without_writing(self):
        _, sale, receipt = self.scenario()
        path = f"/api/v1/shops/{self.shop.id}/sales/{sale['id']}/returns/"
        returned = SaleReturnLine.objects.get(sale_line__sale_id=sale["id"])
        path += f"{returned.sale_return_id}/lines/{returned.id}/receipts/{receipt.id}/decisions/"
        response = self.client.post(path, self.event(kind="restock", quantity_milli=1000,
            is_sellable_confirmed=True), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.make_sale(1000)
        # The new sale already uses current purchase fees; old return cost is still 1000.
        preview = self.client.get(self.path() + "preview/")
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual(preview.data["sales_cost_before"]["total_nanos_fcfa"], "3000000000000")
        self.assertEqual(preview.data["sales_cost_after"]["total_nanos_fcfa"], "3300000000000")
        self.assertEqual(preview.data["quantity_after_milli"], "0")
        self.assertFalse(ReturnCostRevision.objects.exists())

    def test_new_fee_revision_invalidates_preview_even_when_total_is_preserved(self):
        invoice, _, _ = self.scenario()
        preview = self.client.get(self.path() + "preview/")
        self.assertEqual(preview.status_code, 200, preview.content)
        fee = PurchaseCharge.objects.get()
        response = self.client.post(self.url() + invoice["id"] + f"/charges/{fee.id}/revisions/", {
            "operation_id": str(uuid.uuid4()), "reason": "Plan recontrôlé", "base_version": 1,
            "allocations": [{"line_id": invoice["lines"][0]["id"], "amount_fcfa": 200}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        response = self.client.post(self.path(), self.confirmation(preview.data), format="json")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(CostRevaluation.objects.exists())

    def test_seller_cannot_preview_or_confirm_even_with_cost_permission_and_session_is_current(self):
        self.scenario()
        self.authenticate(self.seller)
        Membership.objects.filter(user=self.seller).update(can_read_cost=True)
        self.assertEqual(self.client.get(self.path() + "preview/").status_code, 403)
        self.assertEqual(self.client.post(self.path(), {}, format="json").status_code, 403)
        self.authenticate(self.owner)
        DeviceSession.objects.filter(installation__user=self.owner).update(revoked_at=timezone.now())
        self.assertEqual(self.client.get(self.path() + "preview/").status_code, 401)

    def test_product_of_another_shop_is_not_visible(self):
        other_shop = Shop.objects.create(owner=self.owner, name="Autre boutique")
        other = Product.objects.create(shop=other_shop, name="Autre produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1)
        path = self.path().replace(str(self.product.id), str(other.id))
        self.assertEqual(self.client.get(path + "preview/").status_code, 404)
        self.assertEqual(self.client.post(path, {}, format="json").status_code, 404)

    def test_fee_review_closes_only_after_confirmed_replay_and_original_fee_receipt_is_preserved(self):
        invoice, _, _ = self.scenario()
        fee = PurchaseCharge.objects.get()
        charge_path = self.url() + invoice["id"] + f"/charges/{fee.id}/"
        original = fee.operation.details["response"]
        self.assertTrue(self.client.get(charge_path).data["current_revision"]["historical_costs_pending_review"])
        preview = self.client.get(self.path() + "preview/")
        self.assertTrue(self.client.get(charge_path).data["current_revision"]["historical_costs_pending_review"])
        applied = self.client.post(self.path(), self.confirmation(preview.data), format="json")
        self.assertEqual(applied.status_code, 201, applied.content)
        self.assertFalse(self.client.get(charge_path).data["current_revision"]["historical_costs_pending_review"])
        fee.operation.refresh_from_db()
        self.assertEqual(fee.operation.details["response"], original)
        self.assertTrue(original["current_revision"]["historical_costs_pending_review"])

    def test_legacy_return_without_frozen_cost_remains_visible_as_incomplete_after_replay(self):
        invoice, _, receipt = self.scenario()
        receipt.cost_snapshot.delete()  # Represents a physical receipt predating exact cost snapshots.
        preview = self.client.get(self.path() + "preview/")
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual(preview.data["legacy_return_receipts"], 1)
        applied = self.client.post(self.path(), self.confirmation(preview.data), format="json")
        self.assertEqual(applied.status_code, 201, applied.content)
        fee = PurchaseCharge.objects.get()
        path = self.url() + invoice["id"] + f"/charges/{fee.id}/"
        self.assertTrue(self.client.get(path).data["current_revision"]["historical_costs_pending_review"])

    def test_multi_product_fee_requires_all_products_and_new_allocation_reopens_review(self):
        from purchasing.tests.test_purchase_charge_models import PurchaseChargeModelTests
        document = PurchaseChargeModelTests.invoice(self)
        lines = list(document.lines.select_related("product"))
        received = self.client.post(self.url() + str(document.id) + "/receipts/", self.event(
            lines=[{"line_id": str(line.id), "quantity_milli": 10000} for line in lines]), format="json")
        self.assertEqual(received.status_code, 201, received.content)
        for line in lines:
            self.product = line.product
            self.make_sale(1000)
        charge_path = self.url() + str(document.id) + "/charges/"
        plan = [{"line_id": str(line.id), "amount_fcfa": amount} for line, amount in zip(lines, (1000, 3000), strict=True)]
        fee = self.client.post(charge_path, self.event(label="Transport", amount_fcfa=4000, allocations=plan), format="json")
        self.assertEqual(fee.status_code, 201, fee.content)
        detail = charge_path + fee.data["id"] + "/"

        def replay(line):
            self.product = line.product
            preview = self.client.get(self.path() + "preview/")
            self.assertEqual(preview.status_code, 200, preview.content)
            response = self.client.post(self.path(), self.confirmation(preview.data), format="json")
            self.assertEqual(response.status_code, 201, response.content)

        for version in (1, 2):
            replay(lines[0])
            self.assertTrue(self.client.get(detail).data["current_revision"]["historical_costs_pending_review"])
            replay(lines[1])
            self.assertFalse(self.client.get(detail).data["current_revision"]["historical_costs_pending_review"])
            if version == 1:
                changed = self.client.post(detail + "revisions/", {"operation_id": str(uuid.uuid4()), "base_version": 1,
                    "reason": "Répartition corrigée", "allocations": [{"line_id": str(line.id), "amount_fcfa": 2000} for line in lines]}, format="json")
                self.assertEqual(changed.status_code, 201, changed.content)
                self.assertTrue(self.client.get(detail).data["current_revision"]["historical_costs_pending_review"])

    def test_explicit_legacy_reconstruction_preserves_baseline_and_propagates_to_restock_and_sale(self):
        invoice, sale, receipt = self.scenario()
        original_receipt = receipt.operation.details.copy()
        receipt.cost_snapshot.delete()
        line = SaleReturnLine.objects.get(sale_line__sale_id=sale["id"])
        root = f"/api/v1/shops/{self.shop.id}/sales/{sale['id']}/returns/{line.sale_return_id}/lines/{line.id}/receipts/"
        decision = self.client.post(root + str(receipt.id) + "/decisions/", self.event(kind="restock",
            quantity_milli=1000, is_sellable_confirmed=True), format="json")
        self.assertEqual(decision.status_code, 201, decision.content)
        later = self.make_sale(1000)
        ordinary = self.client.get(self.path() + "preview/")
        self.assertEqual(ordinary.status_code, 200)
        changed_mode = {**self.confirmation(ordinary.data), "reconstruct_legacy_returns": True}
        self.assertEqual(self.client.post(self.path(), changed_mode, format="json").status_code, 409)
        preview = self.client.get(self.path() + "preview/", {"reconstruct_legacy_returns": "true"})
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual(preview.data["reconstructed_return_receipts"], 1)
        self.assertEqual(preview.data["sales_cost_after"]["total_nanos_fcfa"], "3300000000000")
        self.assertFalse(ReturnCostReconstruction.objects.exists())
        body = {**self.confirmation(preview.data), "reconstruct_legacy_returns": True}
        applied = self.client.post(self.path(), body, format="json")
        self.assertEqual(applied.status_code, 201, applied.content)
        baseline = ReturnCostSnapshot.objects.get(receipt=receipt)
        self.assertEqual(baseline.cost_state, "known")
        self.assertEqual(baseline.total_cost_nanos_fcfa, 1000000000000)
        self.assertIsNone(baseline.sale_cost_id)
        self.assertEqual(baseline.reconstruction.batch_id, uuid.UUID(applied.data["id"]))
        revised = ReturnCostRevision.objects.get(origin=baseline)
        self.assertEqual(revised.total_cost_nanos_fcfa, 1100000000000)
        self.assertEqual(StockCostSnapshot.objects.filter(movement__sale_line__sale_id=later["id"]).order_by(
            "-version").first().total_cost_nanos_fcfa, 1100000000000)
        current = self.client.get(root + str(receipt.id) + "/")
        self.assertEqual(current.data["cost"]["origin_kind"], "reconstructed_legacy")
        receipt.operation.refresh_from_db()
        self.assertEqual(receipt.operation.details, original_receipt)
        self.assertEqual(self.client.post(self.path(), body, format="json").data, applied.data)
        self.assertEqual(ReturnCostReconstruction.objects.count(), 1)
        self.assertEqual(StockMovement.objects.count(), 4)
        self.assertEqual(sum(CashMovement.objects.values_list("amount_fcfa", flat=True)), 300)
        fee = PurchaseCharge.objects.get()
        charge_path = self.url() + invoice["id"] + f"/charges/{fee.id}/"
        self.assertFalse(self.client.get(charge_path).data["current_revision"]["historical_costs_pending_review"])

    def test_reconstruction_keeps_unknown_zero_and_provisional_legacy_baselines_distinct(self):
        for legacy, was_provisional, old_state, old_total, revised_state in (
            (None, True, "unknown", None, "known"),
            (0, False, "known", 0, "known"),
            (1000000000, True, "provisional", 1000000000000, "provisional"),
        ):
            with self.subTest(legacy=legacy):
                self.product = Product.objects.create(shop=self.shop, name="Reprise", unit_kind="piece",
                    unit_label="pièce", sale_price_fcfa=100)
                self.invoice_and_receipts(total_price=1200, quantity=2000, parts=(2000,))
                sale = self.make_sale(2000)
                receipt, _ = self.receive(self.return_path(sale, 1000))
                receipt.cost_snapshot.delete()
                ReturnReceipt.objects.filter(id=receipt.id).update(unit_cost_micros=legacy,
                    cost_is_provisional=was_provisional)
                preview = self.client.get(self.path() + "preview/", {"reconstruct_legacy_returns": "true"})
                self.assertEqual(preview.status_code, 200, preview.content)
                applied = self.client.post(self.path(), {**self.confirmation(preview.data),
                    "reconstruct_legacy_returns": True}, format="json")
                self.assertEqual(applied.status_code, 201, applied.content)
                baseline = ReturnCostSnapshot.objects.get(receipt=receipt)
                self.assertEqual((baseline.cost_state, baseline.total_cost_nanos_fcfa), (old_state, old_total))
                current = baseline.revisions.get()
                self.assertEqual(current.total_cost_nanos_fcfa, 1200000000000)
                self.assertEqual(current.cost_state, revised_state)

    def test_legacy_sale_without_snapshot_remains_provisional_after_late_receipt_replay(self):
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        sale = self.make_sale(2000)
        receipt, _ = self.receive(self.return_path(sale, 1000))
        receipt.cost_snapshot.delete()
        StockCostSnapshot.objects.filter(movement__sale_line__sale_id=sale["id"]).delete()
        SaleLine.objects.filter(sale_id=sale["id"]).update(unit_cost_micros=1000000000)
        ReturnReceipt.objects.filter(id=receipt.id).update(unit_cost_micros=1000000000, cost_is_provisional=True)
        # A late historical receipt now covers the stock at the sale's effective date.
        self.invoice_and_receipts(total_price=1000, quantity=1000, parts=(1000,))
        preview = self.client.get(self.path() + "preview/", {"reconstruct_legacy_returns": "true"})
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual(preview.data["sales_cost_after"]["provisional_lines"], 1)
        applied = self.client.post(self.path(), {**self.confirmation(preview.data), "reconstruct_legacy_returns": True}, format="json")
        self.assertEqual(applied.status_code, 201, applied.content)
        cost = StockCostSnapshot.objects.get(movement__sale_line__sale_id=sale["id"])
        self.assertEqual(cost.cost_state, "provisional")
        self.assertEqual(cost.total_cost_nanos_fcfa, 2000000000000)
        self.assertTrue(Sale.objects.get(id=sale["id"]).stock_requires_regularization)

    def test_restock_created_after_reconstruction_records_current_exact_integer_projection(self):
        _, sale, receipt = self.scenario()
        receipt.cost_snapshot.delete()
        preview = self.client.get(self.path() + "preview/", {"reconstruct_legacy_returns": "true"})
        self.assertEqual(preview.status_code, 200)
        applied = self.client.post(self.path(), {**self.confirmation(preview.data), "reconstruct_legacy_returns": True}, format="json")
        self.assertEqual(applied.status_code, 201, applied.content)
        line = SaleReturnLine.objects.get(sale_line__sale_id=sale["id"])
        path = f"/api/v1/shops/{self.shop.id}/sales/{sale['id']}/returns/{line.sale_return_id}/lines/{line.id}/receipts/{receipt.id}/decisions/"
        decision = self.client.post(path, self.event(kind="restock", quantity_milli=1000, is_sellable_confirmed=True), format="json")
        self.assertEqual(decision.status_code, 201, decision.content)
        self.assertEqual(StockMovement.objects.get(id=decision.data["movement_id"]).unit_cost_micros, 1100000000)
        receipt.refresh_from_db()
        self.assertEqual(receipt.unit_cost_micros, 1000000000)
