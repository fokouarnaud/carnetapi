"""Q171 / US-27: prior supplier advances and credits are rights, not new payments."""
import uuid
from datetime import timedelta
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from purchasing.tests import test_purchases as purchase_tests


class SupplierOpeningTests(TestCase):
    setUp = purchase_tests.PurchaseTests.setUp
    authenticate = purchase_tests.PurchaseTests.authenticate

    def test_opening_credit_has_explicit_kind_and_shop_without_a_fake_return(self):
        from common.models import Activity
        from identity.models import DeviceSession
        from purchasing.models import SupplierCredit
        installation = DeviceSession.objects.get(installation__user=self.owner).installation
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="supplier_balance_opened", target_id=uuid.uuid4(), reason="Solde antérieur")
        values = dict(kind="opening_credit", opening_shop=self.shop, opening_supplier_id=self.supplier_id,
                      operation=event, installation=installation, occurred_at=timezone.now(), reference_fcfa=None,
                      amount_fcfa=5000, applied_to_origin_fcfa=0, remaining_fcfa=5000)
        balance = SupplierCredit.objects.create(**values)
        self.assertIsNone(balance.supplier_return_id)
        balance.refresh_from_db()
        self.assertEqual(balance.kind, "opening_credit")
        for changes in ({"opening_shop": None}, {"kind": "return_credit"}, {"reference_fcfa": 0}, {"applied_to_origin_fcfa": 1000}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                SupplierCredit.objects.filter(id=balance.id).update(**changes)

    def url(self, suffix="opening-balances/"):
        return f"/api/v1/shops/{self.shop.id}/suppliers/{self.supplier_id}/" + suffix

    def opening(self, balance_kind="advance", amount=30000):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "balance_kind": balance_kind,
                "amount_fcfa": amount, "occurred_at": "2026-01-01T10:00:00Z", "reason": "Solde antérieur confirmé"}

    def post(self, body):
        return self.client.post(self.url(), body, format="json")

    def debt(self):
        body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "amount_fcfa": 7000,
                "occurred_at": "2026-01-01T10:00:00Z", "reason": "Dette antérieure"}
        response = self.client.post(self.url("debts/"), body, format="json")
        self.assertEqual(response.status_code, 201)
        return response.data

    def test_openings_preserve_debt_advance_and_credit_without_fake_business_or_auto_offset(self):
        from cash.models import CashMovement
        from stock.models import StockMovement
        from purchasing.models import SupplierDebt, SupplierPayment, SupplierReturn, PurchaseDocument
        self.debt()
        advance = self.post(self.opening())
        credit = self.post(self.opening("credit", 5000))
        self.assertEqual(advance.status_code, 201, advance.content)
        self.assertEqual(credit.status_code, 201, credit.content)
        self.assertEqual(advance.data["kind"], "opening_advance")
        self.assertEqual(credit.data["kind"], "opening_credit")
        self.assertIsNone(advance.data["return_id"])
        self.assertIsNone(credit.data["reference_fcfa"])
        self.assertEqual(advance.data["remaining_fcfa"], 30000)
        self.assertEqual(credit.data["remaining_fcfa"], 5000)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 7000)
        self.assertEqual(self.client.get(self.url()).data["count"], 2)
        self.assertEqual(self.client.get(self.url("credits/")).data["count"], 2)
        for model in (CashMovement, StockMovement, SupplierPayment, SupplierReturn, PurchaseDocument):
            self.assertFalse(model.objects.exists(), model.__name__)

    def test_opening_retries_preserve_original_receipt_and_reject_changed_content(self):
        from purchasing.models import SupplierCredit
        body = self.opening()
        first = self.post(body)
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(self.post(body).data, first.data)
        self.assertEqual(self.post({**body, "balance_kind": "credit"}).status_code, 409)
        self.assertEqual(self.post({**body, "operation_id": str(uuid.uuid4())}).status_code, 409)
        self.assertEqual(SupplierCredit.objects.count(), 1)

    def test_allocation_and_real_refund_consume_openings_only_by_explicit_decision(self):
        from cash.models import CashMovement
        from purchasing.models import SupplierCredit, SupplierDebt
        debt = self.debt()
        advance = self.post(self.opening())
        credit = self.post(self.opening("credit", 5000))
        self.assertEqual(advance.status_code, 201)
        self.assertEqual(credit.status_code, 201)
        allocate = {"operation_id": str(uuid.uuid4()), "reason": "Avance affectée", "allocations": [
                    {"debt_id": debt["id"], "amount_fcfa": 3000}]}
        path = self.url(f"credits/{advance.data['id']}/allocate/")
        allocated = self.client.post(path, allocate, format="json")
        self.assertEqual(allocated.status_code, 201, allocated.content)
        self.assertEqual(self.client.post(path, allocate, format="json").data, allocated.data)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 4000)
        self.assertEqual(SupplierCredit.objects.get(id=advance.data["id"]).remaining_fcfa, 27000)
        self.assertFalse(CashMovement.objects.exists())
        refund = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "occurred_at": "2026-01-02T10:00:00Z",
                  "reason": "Remboursement réellement reçu", "method": "orange", "amount_fcfa": 2000}
        path = self.url(f"credits/{credit.data['id']}/refund/")
        refunded = self.client.post(path, refund, format="json")
        self.assertEqual(refunded.status_code, 201, refunded.content)
        self.assertEqual(self.client.post(path, refund, format="json").data, refunded.data)
        self.assertEqual(CashMovement.objects.get().amount_fcfa, 2000)
        self.assertEqual(SupplierCredit.objects.get(id=credit.data["id"]).remaining_fcfa, 3000)
        history = self.client.get(self.url(f"credits/{credit.data['id']}/history/"))
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.data["count"], 2)

    def test_opening_access_is_scoped_and_revoked_or_seller_access_cannot_replay(self):
        from purchasing.models import Supplier
        from identity.models import DeviceSession
        body = self.opening()
        opened = self.post(body)
        self.assertEqual(opened.status_code, 201)
        other = Supplier.objects.create(shop=self.shop, name="Autre fournisseur")
        wrong = f"/api/v1/shops/{self.shop.id}/suppliers/{other.id}/credits/{opened.data['id']}/"
        self.assertEqual(self.client.get(wrong).status_code, 404)
        self.authenticate(self.seller)
        self.assertEqual(self.post(body).status_code, 403)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.assertEqual(self.client.get(self.url("credits/")).status_code, 403)
        self.authenticate(self.owner)
        DeviceSession.objects.filter(installation__user=self.owner).update(revoked_at=timezone.now())
        self.assertEqual(self.post(body).status_code, 401)
        self.assertEqual(self.client.get(self.url()).status_code, 401)

    def test_invalid_openings_do_not_create_claim_or_change_debt(self):
        from purchasing.models import SupplierCredit, SupplierDebt
        self.debt()
        for changes in ({"amount_fcfa": 0}, {"amount_fcfa": -1}, {"amount_fcfa": 1.5},
                        {"balance_kind": "debt"}, {"reason": " "}, {"return_id": str(uuid.uuid4())},
                        {"occurred_at": (timezone.now() + timedelta(days=1)).isoformat()}):
            with self.subTest(changes=changes):
                self.assertEqual(self.post({**self.opening(), **changes}).status_code, 400)
        self.assertFalse(SupplierCredit.objects.exists())
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 7000)
