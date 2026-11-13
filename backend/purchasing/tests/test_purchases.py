"""US-27/28: an order, validated invoice, receipt and money are distinct events."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from cash.models import CashMovement
from identity.models import DeviceSession, Installation, Membership, Shop, User
from catalogue.models import Product, ProductPack
from stock.models import StockMovement


class PurchaseTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="purchase-owner")
        self.seller = User.objects.create_user(username="purchase-seller")
        self.shop = Shop.objects.create(owner=self.owner, name="A")
        Membership.objects.create(shop=self.shop, user=self.seller)
        self.product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=100)
        self.pack = ProductPack.objects.create(product=self.product, name="Carton", base_quantity_milli=24000, sale_price_fcfa=2400)
        self.client = APIClient()
        self.authenticate(self.owner)
        response = self.client.post(f"/api/v1/shops/{self.shop.id}/suppliers/",
                                    {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Fournisseur"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.supplier_id = response.data["id"]

    def authenticate(self, user):
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def url(self, kind="invoices"):
        return f"/api/v1/shops/{self.shop.id}/suppliers/{self.supplier_id}/{kind}/"

    def payload(self, **changes):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-01T00:00:00Z",
                "reason": "Document confirmé", "external_reference": "FOURN-2026-001",
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "pack_id": str(self.pack.id),
                           "quantity_milli": 10000, "unit_price_fcfa": 1000}], **changes}

    def test_order_has_no_debt_and_invoice_creates_10000_debt_without_stock_or_cash(self):
        order = self.client.post(self.url("orders"), self.payload(), format="json")
        self.assertEqual(order.status_code, 201)
        self.assertEqual(order.data["kind"], "order")
        self.assertIsNone(order.data["debt"])
        invoice = self.client.post(self.url(), self.payload(order_id=order.data["id"]), format="json")
        self.assertEqual(invoice.status_code, 201)
        self.assertEqual(invoice.data["kind"], "invoice")
        self.assertEqual(invoice.data["total_fcfa"], 10000)
        self.assertEqual(invoice.data["debt"]["remaining_fcfa"], 10000)
        self.assertEqual(invoice.data["lines"][0]["factor_milli"], 24000)
        self.assertEqual(invoice.data["lines"][0]["base_quantity_milli"], 240000)
        self.assertFalse(StockMovement.objects.exists())
        self.assertFalse(CashMovement.objects.exists())

    def test_invoice_retry_keeps_one_debt_and_frozen_pack_factor_and_price(self):
        data = self.payload()
        invoice = self.client.post(self.url(), data, format="json")
        self.assertEqual(invoice.status_code, 201)
        ProductPack.objects.filter(id=self.pack.id).update(base_quantity_milli=12000, sale_price_fcfa=5000)
        retry = self.client.post(self.url(), data, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, invoice.data)
        current = self.client.get(self.url() + data["id"] + "/")
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.data["lines"][0]["factor_milli"], 24000)
        self.assertEqual(current.data["lines"][0]["unit_price_fcfa"], 1000)
        from purchasing.models import SupplierDebt
        self.assertEqual(SupplierDebt.objects.count(), 1)

    def test_supplier_opening_debt_is_separate_and_idempotent_without_purchase_or_cash(self):
        url = self.url("debts")
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 5000,
                "occurred_at": "2025-12-01T00:00:00Z", "reason": "Dette antérieure"}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data["document_id"])
        self.assertEqual(response.data["remaining_fcfa"], 5000)
        self.assertEqual(self.client.post(url, data, format="json").data, response.data)
        self.assertEqual(self.client.get(url).data["count"], 1)
        self.assertEqual(self.client.get(self.url()).data["count"], 0)
        self.assertFalse(StockMovement.objects.exists())
        self.assertFalse(CashMovement.objects.exists())

    def test_seller_cannot_validate_invoice_even_if_they_can_read_costs(self):
        Membership.objects.filter(user=self.seller).update(can_read_cost=True)
        self.authenticate(self.seller)
        self.assertEqual(self.client.post(self.url(), self.payload(), format="json").status_code, 403)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.assertFalse(CashMovement.objects.exists())

    def test_changed_retry_cannot_replace_original_invoice_or_debt(self):
        from purchasing.models import PurchaseDocument, SupplierDebt
        data = self.payload()
        original = self.client.post(self.url(), data, format="json")
        self.assertEqual(original.status_code, 201)
        data["lines"][0]["unit_price_fcfa"] = 2000
        self.assertEqual(self.client.post(self.url(), data, format="json").status_code, 409)
        self.assertEqual(PurchaseDocument.objects.get().total_fcfa, 10000)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 10000)

    def test_foreign_product_in_second_line_rolls_back_entire_invoice(self):
        from purchasing.models import PurchaseDocument, SupplierDebt
        other = Shop.objects.create(owner=self.owner, name="B")
        foreign = Product.objects.create(shop=other, name="Étranger", unit_kind="piece", unit_label="pièce", sale_price_fcfa=100)
        data = self.payload()
        data["lines"].append({"id": str(uuid.uuid4()), "product_id": str(foreign.id), "quantity_milli": 1000, "unit_price_fcfa": 100})
        self.assertEqual(self.client.post(self.url(), data, format="json").status_code, 404)
        self.assertFalse(PurchaseDocument.objects.exists())
        self.assertFalse(SupplierDebt.objects.exists())

    def test_database_rejects_invoice_line_with_incorrect_base_quantity_or_total(self):
        from django.db import IntegrityError, transaction
        from purchasing.models import PurchaseLine
        self.assertEqual(self.client.post(self.url(), self.payload(), format="json").status_code, 201)
        line = PurchaseLine.objects.get()
        for changes in ({"base_quantity_milli": 239999}, {"total_fcfa": 9999},
                        {"quantity_milli": 1001, "factor_milli": 1001, "base_quantity_milli": 1002, "total_fcfa": 1001}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                PurchaseLine.objects.filter(id=line.id).update(**changes)

    def test_large_exact_invoice_amount_and_half_up_rounding_survive_database_constraints(self):
        from sales.services.pricing import MAX_FCFA
        data = self.payload()
        data["lines"][0].update(quantity_milli=1000, unit_price_fcfa=MAX_FCFA)
        result = self.client.post(self.url(), data, format="json")
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.data["total_fcfa"], MAX_FCFA)
        Product.objects.filter(id=self.product.id).update(unit_kind="weight")
        data = self.payload()
        data["lines"][0].update(pack_id=None, quantity_milli=500, unit_price_fcfa=249)
        result = self.client.post(self.url(), data, format="json")
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.data["total_fcfa"], 125)

    def receipt_payload(self, invoice, quantity=6000):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-02T00:00:00Z", "backdate_reason": "Reprise historique",
                "reason": "Marchandises comptées", "lines": [{"line_id": invoice["lines"][0]["id"], "quantity_milli": quantity}]}

    def test_partial_receipt_adds_only_six_cartons_and_retry_never_adds_twice(self):
        invoice = self.client.post(self.url(), self.payload(), format="json")
        self.assertEqual(invoice.status_code, 201)
        url = self.url() + invoice.data["id"] + "/receipts/"
        data = self.receipt_payload(invoice.data)
        ProductPack.objects.filter(id=self.pack.id).update(base_quantity_milli=12000)
        result = self.client.post(url, data, format="json")
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.data["lines"][0]["base_quantity_milli"], 144000)
        retry = self.client.post(url, data, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(result.data, retry.data)
        self.assertEqual(StockMovement.objects.get().quantity_milli, 144000)
        detail = self.client.get(self.url() + invoice.data["id"] + "/")
        self.assertEqual(detail.data["lines"][0]["received_quantity_milli"], 6000)
        self.assertEqual(detail.data["lines"][0]["awaiting_quantity_milli"], 4000)
        self.assertEqual(detail.data["debt"]["remaining_fcfa"], 10000)
        self.assertFalse(CashMovement.objects.exists())

    def test_receipt_over_remaining_quantity_is_rejected_without_second_stock_effect(self):
        invoice = self.client.post(self.url(), self.payload(), format="json")
        self.assertEqual(invoice.status_code, 201)
        url = self.url() + invoice.data["id"] + "/receipts/"
        self.assertEqual(self.client.post(url, self.receipt_payload(invoice.data), format="json").status_code, 201)
        self.assertEqual(self.client.post(url, self.receipt_payload(invoice.data, 5000), format="json").status_code, 400)
        self.assertEqual(StockMovement.objects.count(), 1)
        self.assertEqual(StockMovement.objects.get().quantity_milli, 144000)

    def test_seller_cannot_receive_goods_even_with_cost_permission(self):
        invoice = self.client.post(self.url(), self.payload(), format="json")
        self.assertEqual(invoice.status_code, 201)
        self.authenticate(self.seller)
        url = self.url() + invoice.data["id"] + "/receipts/"
        self.assertEqual(self.client.post(url, self.receipt_payload(invoice.data), format="json").status_code, 403)
        self.assertFalse(StockMovement.objects.exists())
