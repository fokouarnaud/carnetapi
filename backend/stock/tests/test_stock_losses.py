"""US-14: a confirmed physical loss changes stock, never money or sales."""
import uuid
from django.test import TestCase
from cash.models import CashMovement
from customers.models import CustomerDebt
from sales.models import Sale
from stock.models import StockMovement
from catalogue.tests import test_catalogue


class StockLossTests(TestCase):
    setUp = test_catalogue.CatalogueTests.setUp
    authenticate = test_catalogue.CatalogueTests.authenticate
    product_payload = test_catalogue.CatalogueTests.payload

    def prepare(self, unit_kind="piece", quantity=10000):
        data = self.product_payload(unit_kind=unit_kind)
        data["opening"]["quantity_milli"] = quantity
        result = self.client.post(f"/api/v1/shops/{self.shop.id}/products/", data, format="json")
        self.assertEqual(result.status_code, 201)
        return result.data

    def url(self, product):
        return f"/api/v1/shops/{self.shop.id}/products/{product['id']}/losses/"

    def payload(self, quantity=2000):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "quantity_milli": quantity,
                "occurred_at": "2026-01-02T10:00:00Z", "backdate_reason": "Reprise historique", "reason": "Deux unités cassées"}

    def test_recorded_loss_is_durable_idempotent_and_has_no_financial_or_sales_effect(self):
        product = self.prepare()
        data = self.payload()
        loss = self.client.post(self.url(product), data, format="json")
        self.assertEqual(loss.status_code, 201)
        self.assertEqual(loss.data["quantity_milli"], 2000)
        self.assertEqual(loss.data["stock_after_milli"], 8000)
        self.assertEqual(loss.data["actor_id"], str(self.owner.id))
        self.assertEqual(loss.data["reason"], data["reason"])
        self.assertFalse(loss.data["stock_requires_regularization"])
        retry = self.client.post(self.url(product), data, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, loss.data)
        self.assertEqual(StockMovement.objects.get(kind="loss").quantity_milli, -2000)
        self.assertEqual(self.client.get(self.url(product)).data["count"], 1)
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(CustomerDebt.objects.exists())

    def test_true_loss_larger_than_known_stock_is_preserved_and_requires_regularization(self):
        product = self.prepare()
        loss = self.client.post(self.url(product), self.payload(15000), format="json")
        self.assertEqual(loss.status_code, 201)
        self.assertEqual(loss.data["stock_after_milli"], -5000)
        self.assertTrue(loss.data["stock_requires_regularization"])
        self.assertEqual(StockMovement.objects.get(kind="loss").quantity_milli, -15000)

    def test_seller_cannot_record_or_read_loss_and_piece_fraction_is_refused(self):
        product = self.prepare()
        self.assertEqual(self.client.post(self.url(product), self.payload(1500), format="json").status_code, 400)
        self.authenticate(self.seller)
        self.assertEqual(self.client.post(self.url(product), self.payload(), format="json").status_code, 403)
        self.assertEqual(self.client.get(self.url(product)).status_code, 403)
        self.assertFalse(StockMovement.objects.filter(kind="loss").exists())

    def test_weight_fraction_is_exact_and_changed_retry_cannot_deduct_more_stock(self):
        product = self.prepare(unit_kind="weight")
        data = self.payload(125)
        loss = self.client.post(self.url(product), data, format="json")
        self.assertEqual(loss.status_code, 201)
        self.assertEqual(loss.data["stock_after_milli"], 9875)
        self.assertEqual(self.client.post(self.url(product), {**data, "quantity_milli": 250}, format="json").status_code, 409)
        self.assertEqual(StockMovement.objects.get(kind="loss").quantity_milli, -125)

    def test_database_rejects_piece_fraction_unknown_unit_and_incorrect_regularization_flag(self):
        from django.db import IntegrityError, transaction
        from stock.models import StockLoss
        product = self.prepare()
        self.assertEqual(self.client.post(self.url(product), self.payload(), format="json").status_code, 201)
        for changes in ({"quantity_milli": 1500}, {"unit_kind": "unknown"}, {"stock_requires_regularization": True},
                        {"stock_after_milli": -1, "stock_requires_regularization": False}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                StockLoss.objects.update(**changes)

    def test_foreign_product_and_invalid_loss_input_have_no_effect(self):
        product = self.prepare()
        wrong = f"/api/v1/shops/{self.other.id}/products/{product['id']}/losses/"
        self.assertEqual(self.client.post(wrong, self.payload(), format="json").status_code, 404)
        for quantity in (0, -1000, True, 1000.5, "1000"):
            with self.subTest(quantity=quantity):
                self.assertEqual(self.client.post(self.url(product), self.payload(quantity), format="json").status_code, 400)
        self.assertFalse(StockMovement.objects.filter(kind="loss").exists())

    def test_exact_stock_snapshot_beyond_int64_survives_a_later_loss_and_retry(self):
        from common.models import Activity
        from sales.services.pricing import MAX_FCFA
        product = self.prepare(unit_kind="weight", quantity=MAX_FCFA)
        # Seed a second physical arrival to exercise aggregate storage, not invoice pricing.
        arrival = Activity.objects.create(shop=self.shop, actor=self.owner, action="arrival_fixture", target_id=product["id"], reason="Deuxième arrivée")
        StockMovement.objects.create(product_id=product["id"], actor=self.owner, operation=arrival,
                                    kind="purchase_receipt", quantity_milli=MAX_FCFA,
                                    occurred_at="2026-01-01T11:00:00Z", reason="Deuxième arrivée")
        data = self.payload(125)
        loss = self.client.post(self.url(product), data, format="json")
        self.assertEqual(loss.status_code, 201)
        self.assertEqual(loss.data["stock_after_milli"], 18446744073709551489)
        self.assertFalse(loss.data["stock_requires_regularization"])
        next_loss = self.client.post(self.url(product), self.payload(250), format="json")
        self.assertEqual(next_loss.status_code, 201)
        self.assertEqual(next_loss.data["stock_after_milli"], 18446744073709551239)
        retry = self.client.post(self.url(product), data, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, loss.data)
