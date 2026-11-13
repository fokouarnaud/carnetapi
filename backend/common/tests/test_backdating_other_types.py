"""US-35 / G15: losses, goods receipts and supplier payments share the 30-day backdating bound."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone

from identity.models import Membership, User
from purchasing.tests import test_purchases
from stock.tests import test_stock_losses


def days_ago(days):
    return (timezone.now() - timedelta(days=days)).isoformat()


class LossBackdatingTests(TestCase):
    setUp = test_stock_losses.StockLossTests.setUp
    authenticate = test_stock_losses.StockLossTests.authenticate
    product_payload = test_stock_losses.StockLossTests.product_payload
    prepare = test_stock_losses.StockLossTests.prepare
    url = test_stock_losses.StockLossTests.url
    payload = test_stock_losses.StockLossTests.payload

    def test_member_manager_loss_beyond_thirty_days_is_refused(self):
        product = self.prepare()
        manager = User.objects.create_user(username="loss-manager")
        Membership.objects.create(shop=self.shop, user=manager, role=Membership.Role.MANAGER)
        self.authenticate(manager)
        recent = {**self.payload(), "occurred_at": days_ago(5)}
        self.assertEqual(self.client.post(self.url(product), recent, format="json").status_code, 201)
        old = {**self.payload(), "occurred_at": days_ago(31)}
        self.assertEqual(self.client.post(self.url(product), old, format="json").status_code, 400)


class PurchaseBackdatingTests(TestCase):
    setUp = test_purchases.PurchaseTests.setUp
    authenticate = test_purchases.PurchaseTests.authenticate
    url = test_purchases.PurchaseTests.url
    payload = test_purchases.PurchaseTests.payload
    receipt_payload = test_purchases.PurchaseTests.receipt_payload

    def test_owner_receipt_beyond_thirty_days_needs_second_reason(self):
        invoice = self.client.post(self.url(), self.payload(occurred_at=days_ago(1)), format="json")
        self.assertEqual(invoice.status_code, 201)
        url = self.url() + invoice.data["id"] + "/receipts/"
        old = {**self.receipt_payload(invoice.data, quantity=1000), "occurred_at": days_ago(40)}
        old.pop("backdate_reason", None)  # the shared historical helper carries one; this case must not
        self.assertEqual(self.client.post(url, old, format="json").status_code, 400)
        old.update(id=str(uuid.uuid4()), operation_id=str(uuid.uuid4()), backdate_reason="Bon de livraison retrouvé")
        self.assertEqual(self.client.post(url, old, format="json").status_code, 201)

    def test_owner_supplier_payment_beyond_thirty_days_needs_second_reason(self):
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": days_ago(45),
                "reason": "Avance versée", "parts": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 1000}]}
        self.assertEqual(self.client.post(self.url("payments"), data, format="json").status_code, 400)
        data.update(id=str(uuid.uuid4()), operation_id=str(uuid.uuid4()), backdate_reason="Reçu retrouvé")
        self.assertEqual(self.client.post(self.url("payments"), data, format="json").status_code, 201)
