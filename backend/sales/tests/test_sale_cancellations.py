"""US-26: cancel only the uninverted remainder and preserve original physical/cash facts."""
import uuid
from django.test import TestCase
from django.utils import timezone
from sales.tests import test_sale_returns as return_tests
from common.models import Activity
from cash.models import CashMovement
from customers.models import Customer, CustomerCredit, CustomerDebt
from identity.models import Membership
from catalogue.models import Product, ProductPack
from stock.models import ReturnReceipt, StockMovement
from sales.models import SaleCancellation, SaleLine, SalePayment, SaleReturn


class SaleCancellationTests(TestCase):
    setUp = return_tests.SaleReturnTests.setUp
    authenticate = return_tests.SaleReturnTests.authenticate
    payload = return_tests.SaleReturnTests.payload
    url = return_tests.SaleReturnTests.url
    prepare_sale = return_tests.SaleReturnTests.prepare_sale
    return_url = return_tests.SaleReturnTests.return_url
    return_payload = return_tests.SaleReturnTests.return_payload

    def cancellation_url(self, sale):
        return self.url(sale["id"]) + "cancellation/"

    def cancellation_payload(self):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(), "reason": "Vente saisie par erreur"}

    def test_partial_return_then_cancellation_reverses_only_remainder_without_automatic_cash_or_stock(self):
        sale = self.prepare_sale()
        earlier_data = self.return_payload(sale)
        earlier = self.client.post(self.return_url(sale), earlier_data, format="json")
        self.assertEqual(earlier.status_code, 201)
        refund = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
            "reason": "Remboursement réel", "method": "cash", "amount_fcfa": 3000}
        self.assertEqual(self.client.post(self.return_url(sale) + earlier.data["id"] + "/refunds/", refund, format="json").status_code, 201)
        data = self.cancellation_payload()
        cancelled = self.client.post(self.cancellation_url(sale), data, format="json")
        self.assertEqual(cancelled.status_code, 201, cancelled.content)
        self.assertEqual(self.client.post(self.cancellation_url(sale), data, format="json").data, cancelled.data)
        origin = SaleCancellation.objects.get().sale_return
        self.assertEqual(origin.amount_fcfa, 5000)
        self.assertEqual(origin.lines.get().quantity_milli, 1000)
        self.assertEqual(SaleReturn.objects.count(), 2)
        self.assertEqual(CashMovement.objects.count(), 3)
        self.assertEqual(StockMovement.objects.count(), 2)
        self.assertEqual(self.client.post(self.return_url(sale), earlier_data, format="json").status_code, 200)
        self.assertEqual(self.client.post(self.return_url(sale), self.return_payload(sale), format="json").status_code, 409)
        current = self.client.get(self.url(sale["id"]))
        self.assertEqual(current.data["state"], "cancelled")
        self.assertEqual(current.data["cancellation"]["id"], data["id"])
        self.assertEqual(current.data["id"], sale["id"])
        self.assertEqual(current.data["net_total_fcfa"], 10000)
        self.assertEqual(current.data["lines"], sale["lines"])
        receipt_url = self.return_url(sale) + str(origin.id) + "/lines/" + str(origin.lines.get().id) + "/receipts/"
        physical = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": timezone.now().isoformat(),
            "reason": "Une pièce vendable reçue", "condition": "sellable", "quantity_milli": 1000}
        self.assertEqual(self.client.post(receipt_url, physical, format="json").status_code, 201)
        self.assertEqual(StockMovement.objects.get(kind="customer_return").quantity_milli, 1000)
        history = self.client.get(self.url(sale["id"]) + "history/")
        self.assertIn("sale_cancelled", [row["action"] for row in history.data["results"]])
        self.assertEqual(self.client.get(self.cancellation_url(sale)).data["reason"], data["reason"])

    def test_credit_cancellation_reduces_origin_debt_and_creates_separate_refundable_claim(self):
        customer = Customer.objects.create(shop=self.shop, name="Client")
        sale = self.prepare_sale(mode="credit", customer_id=str(customer.id), payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 4000}])
        response = self.client.post(self.cancellation_url(sale), self.cancellation_payload(), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        returned = SaleCancellation.objects.get().sale_return
        self.assertEqual(returned.amount_fcfa, 10000)
        self.assertEqual(returned.debt_reduced_fcfa, 6000)
        self.assertEqual(returned.credit_fcfa, 4000)
        self.assertEqual(CustomerDebt.objects.get().remaining_fcfa, 0)
        self.assertEqual(SalePayment.objects.get().amount_fcfa, 4000)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 4000)
        statement = self.client.get(f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/statement/").data
        self.assertEqual(statement["closing_balance_fcfa"], -4000)
        self.assertEqual(statement["closing_unallocated_fcfa"], 4000)
        self.assertFalse(ReturnReceipt.objects.exists())

    def test_already_fully_returned_sale_cancels_without_second_financial_return(self):
        sale = self.prepare_sale()
        self.assertEqual(self.client.post(self.return_url(sale), self.return_payload(sale, 2000), format="json").status_code, 201)
        response = self.client.post(self.cancellation_url(sale), self.cancellation_payload(), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIsNone(response.data["sale_return_id"])
        self.assertEqual(SaleReturn.objects.count(), 1)
        self.assertEqual(CashMovement.objects.count(), 2)
        self.assertEqual(StockMovement.objects.count(), 2)
        self.assertEqual(self.client.post(self.cancellation_url(sale), self.cancellation_payload(), format="json").status_code, 409)

    def test_seller_foreign_sale_invalid_input_and_changed_retry_are_refused(self):
        sale = self.prepare_sale()
        path = self.cancellation_url(sale)
        data = self.cancellation_payload()
        self.authenticate(self.seller)
        self.assertEqual(self.client.post(path, data, format="json").status_code, 403)
        self.assertEqual(self.client.get(path).status_code, 403)
        self.authenticate(self.owner)
        self.assertEqual(self.client.post(path.replace(str(self.shop.id), str(self.other.id)), data, format="json").status_code, 404)
        for changes in ({"reason": " "}, {"occurred_at": "2026-01-01T09:00:00Z"}, {"quantity_milli": 1000}):
            with self.subTest(changes=changes):
                self.assertEqual(self.client.post(path, {**data, **changes}, format="json").status_code, 400)
        self.assertFalse(SaleCancellation.objects.exists())
        self.assertEqual(self.client.post(path, data, format="json").status_code, 201)
        self.assertEqual(self.client.post(path, {**data, "reason": "Autre motif"}, format="json").status_code, 409)
        self.authenticate(self.seller)
        current = self.client.get(self.url(sale["id"]))
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.data["state"], "cancelled")
        self.assertNotIn("reason", current.data["cancellation"])

    def test_zero_net_sale_still_keeps_physical_remainder_and_child_operation_collision_is_atomic(self):
        sale = self.prepare_sale(global_discount_fcfa=10000, payments=[])
        data = self.cancellation_payload()
        data["operation_id"] = str(uuid.uuid5(uuid.UUID(data["id"]), "cancellation-return-operation"))
        self.assertEqual(self.client.post(self.cancellation_url(sale), data, format="json").status_code, 409)
        self.assertFalse(SaleCancellation.objects.exists())
        self.assertFalse(SaleReturn.objects.exists())
        data["operation_id"] = str(uuid.uuid4())
        response = self.client.post(self.cancellation_url(sale), data, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        returned = SaleCancellation.objects.get().sale_return
        self.assertEqual(returned.amount_fcfa, 0)
        self.assertEqual(returned.lines.get().quantity_milli, 2000)
        self.assertFalse(CashMovement.objects.exists())

    def test_approved_cumulative_rounding_leaves_67_after_a_33_return(self):
        sale = self.prepare_sale(global_discount_fcfa=14900,
            lines=[{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1, "quantity_milli": 3000}],
            payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 100}])
        first = self.client.post(self.return_url(sale), self.return_payload(sale), format="json")
        self.assertEqual(first.data["amount_fcfa"], 33)
        response = self.client.post(self.cancellation_url(sale), self.cancellation_payload(), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        returned = SaleCancellation.objects.get().sale_return
        self.assertEqual(returned.amount_fcfa, 67)
        self.assertEqual(returned.lines.get().quantity_milli, 2000)
        self.assertEqual(sum(SaleReturn.objects.values_list("amount_fcfa", flat=True)), 100)

    def test_pack_cancellation_uses_original_factor_and_net_after_catalogue_changes(self):
        pack = ProductPack.objects.create(product=self.product, name="Carton", base_quantity_milli=24000, sale_price_fcfa=4800)
        sale = self.prepare_sale(lines=[{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1,
            "pack_id": str(pack.id), "quantity_milli": 1000}], payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 4800}])
        first = self.client.post(self.return_url(sale), self.return_payload(sale, 3000), format="json")
        self.assertEqual(first.data["amount_fcfa"], 600)
        ProductPack.objects.filter(id=pack.id).update(base_quantity_milli=12000, sale_price_fcfa=2600, is_active=False)
        Product.objects.filter(id=self.product.id).update(is_active=False)
        response = self.client.post(self.cancellation_url(sale), self.cancellation_payload(), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        returned = SaleCancellation.objects.get().sale_return
        self.assertEqual(returned.lines.get().quantity_milli, 21000)
        self.assertEqual(returned.amount_fcfa, 4200)
        self.assertEqual(SaleLine.objects.get().factor_milli, 24000)
        self.assertEqual(StockMovement.objects.filter(kind="sale").count(), 1)

    def test_existing_change_due_remains_separate_from_cancellation_credit(self):
        customer = Customer.objects.create(shop=self.shop, name="Client")
        sale = self.prepare_sale(customer_id=str(customer.id), lines=[{"id": str(uuid.uuid4()), "product_id": str(self.product.id),
            "product_version": 1, "quantity_milli": 1000}], payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 6000}])
        response = self.client.post(self.cancellation_url(sale), self.cancellation_payload(), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(CustomerCredit.objects.get().remaining_fcfa, 1000)
        self.assertEqual(SaleCancellation.objects.get().sale_return.credit_fcfa, 5000)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 6000)
        statement = self.client.get(f"/api/v1/shops/{self.shop.id}/clients/{customer.id}/statement/").data
        self.assertEqual(statement["closing_balance_fcfa"], -6000)
        self.assertEqual(statement["closing_unallocated_fcfa"], 6000)

    def test_chronology_conflict_is_explicit_and_manager_demotion_blocks_old_receipt(self):
        sale = self.prepare_sale()
        self.assertEqual(self.client.post(self.return_url(sale), self.return_payload(sale), format="json").status_code, 201)
        data = self.cancellation_payload()
        conflicting = {**data, "occurred_at": sale["occurred_at"]}
        response = self.client.post(self.cancellation_url(sale), conflicting, format="json", HTTP_ACCEPT_LANGUAGE="en")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "cancellation_timeline_conflict")
        self.assertEqual(response.json()["detail"], "Cancellation precedes an existing return. Check the chronology.")
        self.assertFalse(SaleCancellation.objects.exists())
        self.member.role = Membership.Role.MANAGER
        self.member.save(update_fields=["role"])
        self.authenticate(self.seller)
        self.assertEqual(self.client.post(self.cancellation_url(sale), data, format="json").status_code, 201)
        self.member.role = Membership.Role.SELLER
        self.member.save(update_fields=["role"])
        self.assertEqual(self.client.post(self.cancellation_url(sale), data, format="json").status_code, 403)

    def test_generated_child_line_collision_rolls_back_parent_audit_and_keeps_earlier_return(self):
        sale = self.prepare_sale()
        data = self.cancellation_payload()
        earlier = self.return_payload(sale)
        earlier["lines"][0]["id"] = str(uuid.uuid5(uuid.UUID(data["id"]), "cancellation-line:" + sale["lines"][0]["id"]))
        self.assertEqual(self.client.post(self.return_url(sale), earlier, format="json").status_code, 201)
        data["occurred_at"] = timezone.now().isoformat()
        response = self.client.post(self.cancellation_url(sale), data, format="json")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(SaleCancellation.objects.exists())
        self.assertFalse(Activity.objects.filter(id=data["operation_id"]).exists())
        self.assertEqual(SaleReturn.objects.count(), 1)
        self.assertEqual(CashMovement.objects.count(), 2)
