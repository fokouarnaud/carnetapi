"""US-28 / G17: the manager explicitly closes an unreceived invoice remainder; the debt is unchanged."""
from django.db import IntegrityError, transaction
from django.test import TestCase

from common.models import Activity
from identity.models import Installation
from purchasing.models import PurchaseLine, SupplierDebt
from purchasing.tests import test_purchases


class PurchaseRemainderClosureTests(TestCase):
    setUp = test_purchases.PurchaseTests.setUp
    authenticate = test_purchases.PurchaseTests.authenticate
    url = test_purchases.PurchaseTests.url
    payload = test_purchases.PurchaseTests.payload
    receipt_payload = test_purchases.PurchaseTests.receipt_payload

    def close(self, line, quantity):
        from purchasing.models import PurchaseLineClosure
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="purchase_remainder_closed",
                                        target_id=line.id, reason="Reliquat abandonné")
        return PurchaseLineClosure.objects.create(line=line, operation=event, quantity_milli=quantity,
                                                  installation=Installation.objects.create(user=self.owner),
                                                  reason="Le fournisseur ne livrera pas le reste")

    def test_closed_remainder_is_no_longer_awaited_and_blocks_later_receipt_without_debt_change(self):
        invoice = self.client.post(self.url(), self.payload(), format="json").data
        url = self.url() + invoice["id"] + "/receipts/"
        self.assertEqual(self.client.post(url, self.receipt_payload(invoice), format="json").status_code, 201)
        self.close(PurchaseLine.objects.get(id=invoice["lines"][0]["id"]), 4000)
        detail = self.client.get(self.url() + invoice["id"] + "/").data
        self.assertEqual(detail["lines"][0]["awaiting_quantity_milli"], 0)
        self.assertEqual(self.client.post(url, self.receipt_payload(invoice, quantity=1000), format="json").status_code, 400)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 10000)

    def test_closure_quantity_must_be_positive(self):
        invoice = self.client.post(self.url(), self.payload(), format="json").data
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.close(PurchaseLine.objects.get(id=invoice["lines"][0]["id"]), 0)


class PurchaseRemainderClosureApiTests(PurchaseRemainderClosureTests):
    def test_manager_closes_the_unreceived_remainder_through_the_api_once(self):
        import uuid
        invoice = self.client.post(self.url(), self.payload(), format="json").data
        self.client.post(self.url() + invoice["id"] + "/receipts/", self.receipt_payload(invoice), format="json")
        url = self.url() + invoice["id"] + f"/lines/{invoice['lines'][0]['id']}/close/"
        data = {"operation_id": str(uuid.uuid4()), "reason": "Le fournisseur ne livrera pas le reste"}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["quantity_milli"], 4000)
        self.assertEqual(self.client.post(url, data, format="json").status_code, 200)
        self.assertEqual(self.client.post(url, {**data, "operation_id": str(uuid.uuid4())}, format="json").status_code, 409)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 10000)
