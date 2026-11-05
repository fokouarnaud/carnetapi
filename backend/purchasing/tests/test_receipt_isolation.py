"""Reject inconsistent legacy relations before exposing cost evidence or stock."""
import uuid

from django.test import TestCase

from catalogue.models import Product, ProductPack
from common.models import Activity
from identity.models import Shop
from purchasing.models import PurchaseDocument, PurchaseLine, PurchaseReceipt, Supplier
from purchasing.tests import test_purchases as fixtures
from stock.models import StockMovement


class ReceiptIsolationTests(TestCase):
    setUp = fixtures.PurchaseTests.setUp
    authenticate = fixtures.PurchaseTests.authenticate
    url = fixtures.PurchaseTests.url
    payload = fixtures.PurchaseTests.payload

    def test_inconsistent_document_or_line_cannot_leak_costs_or_increase_foreign_stock(self):
        other = Shop.objects.create(owner=self.owner, name="B")
        supplier = Supplier.objects.create(shop=other, name="Fournisseur privé B")
        product = Product.objects.create(shop=other, name="Produit privé B", unit_kind="piece",
                                         unit_label="pièce", sale_price_fcfa=1)
        pack = ProductPack.objects.create(product=product, name="Carton privé B",
                                          base_quantity_milli=24000, sale_price_fcfa=1)
        for relation in ("supplier", "operation", "product", "pack"):
            with self.subTest(relation=relation):
                created = self.client.post(self.url(), self.payload(), format="json")
                self.assertEqual(created.status_code, 201, created.content)
                document = PurchaseDocument.objects.get(id=created.data["id"])
                line = PurchaseLine.objects.get(document=document)
                if relation == "supplier":
                    PurchaseDocument.objects.filter(id=document.id).update(supplier=supplier)
                elif relation == "operation":
                    Activity.objects.filter(id=document.operation_id).update(shop=other)
                elif relation == "product":
                    PurchaseLine.objects.filter(id=line.id).update(product=product)
                else:
                    PurchaseLine.objects.filter(id=line.id).update(pack=pack)
                endpoint = self.url() + str(document.id) + "/receipts/"
                body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                        "occurred_at": "2026-10-24T12:00:00Z", "reason": "Réception physique",
                        "lines": [{"line_id": str(line.id), "quantity_milli": 1000}]}
                expected = 404 if relation in ("supplier", "operation") else 409
                for response in (self.client.get(endpoint), self.client.post(endpoint, body, format="json")):
                    self.assertEqual(response.status_code, expected, response.content)
                    self.assertNotIn("privé B", response.content.decode())
                self.assertFalse(PurchaseReceipt.objects.exists())
                self.assertFalse(StockMovement.objects.exists())
