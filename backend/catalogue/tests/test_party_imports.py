"""US-40: typed customer/supplier CSV imports and distinct opening balances."""
import hashlib
import uuid
from django.db import IntegrityError, transaction
from django.test import TestCase
from stock.tests import test_stock_thresholds as threshold_tests


class PartyImportTests(TestCase):
    setUp = threshold_tests.StockThresholdApiTests.setUp
    authenticate = threshold_tests.StockThresholdApiTests.authenticate

    def test_import_kind_is_persisted_and_unknown_types_are_refused(self):
        from catalogue.models import CatalogueImport
        from identity.models import DeviceSession
        source = b"name,phone\nClient,\n"
        installation = DeviceSession.objects.get(installation__user=self.owner).installation
        record = CatalogueImport.objects.create(kind="customers", shop=self.shop, actor=self.owner,
            installation=installation, source=source, source_sha256=hashlib.sha256(source).hexdigest())
        record.refresh_from_db()
        self.assertEqual(record.kind, "customers")
        with self.assertRaises(IntegrityError), transaction.atomic():
            CatalogueImport.objects.filter(id=record.id).update(kind="unknown")

    def url(self, kind="customers", suffix="", shop=None):
        prefix = "customer-imports" if kind == "customers" else "supplier-imports"
        return f"/api/v1/shops/{(shop or self.shop).id}/{prefix}/" + suffix

    def upload(self, source, kind="customers", identifier=None):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.client.post(self.url(kind), {"id": identifier or str(uuid.uuid4()),
            "file": SimpleUploadedFile("tiers.csv", source, content_type="text/csv")}, format="multipart")

    def confirm(self, preview, kind="customers", body=None):
        return self.client.post(self.url(kind, str(preview["id"]) + "/confirm/"), body or {
            "operation_id": str(uuid.uuid4()), "source_sha256": preview["source_sha256"], "reason": "Soldes vérifiés"}, format="json")

    def test_customer_import_preserves_duplicate_names_and_phones_and_distinct_opening_debt(self):
        from customers.models import Customer, CustomerDebt, CustomerLedgerEntry
        from sales.models import Sale
        from cash.models import CashMovement
        from stock.models import StockMovement
        source = ("name,phone,opening_debt_fcfa,opening_occurred_at,opening_reason,due_date\n"
                  "Amina,+237699123456,3000,2026-01-01T10:00:00Z,Créance antérieure,2026-01-31\n"
                  "Amina,+237699123456,,,,\n").encode()
        preview = self.upload(source)
        self.assertEqual(preview.status_code, 201, preview.content)
        self.assertFalse(preview.data["has_errors"], preview.data)
        self.assertEqual(Customer.objects.count(), 0)
        applied = self.confirm(preview.data)
        self.assertEqual(applied.status_code, 200, applied.content)
        self.assertEqual(applied.data["created_count"], 2)
        self.assertEqual(len(applied.data["customer_ids"]), 2)
        self.assertEqual(Customer.objects.count(), 2)
        self.assertEqual(CustomerDebt.objects.get().amount_fcfa, 3000)
        self.assertIsNone(CustomerDebt.objects.get().sale_id)
        self.assertEqual(CustomerLedgerEntry.objects.get().debt_delta_fcfa, 3000)
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

    def test_supplier_import_opening_debt_creates_no_invoice_receipt_or_payment(self):
        from purchasing.models import Supplier, SupplierDebt, PurchaseDocument, PurchaseReceipt
        from cash.models import CashMovement
        source = b"name,phone,opening_debt_fcfa,opening_occurred_at,opening_reason\nFournisseur,,7000,2026-01-01T10:00:00Z,Solde anterieur\n"
        preview = self.upload(source, "suppliers")
        self.assertEqual(preview.status_code, 201, preview.content)
        self.assertFalse(preview.data["has_errors"], preview.data)
        applied = self.confirm(preview.data, "suppliers")
        self.assertEqual(applied.status_code, 200, applied.content)
        self.assertEqual(len(applied.data["supplier_ids"]), 1)
        self.assertEqual(Supplier.objects.count(), 1)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 7000)
        self.assertIsNone(SupplierDebt.objects.get().document_id)
        self.assertFalse(PurchaseDocument.objects.exists())
        self.assertFalse(PurchaseReceipt.objects.exists())
        self.assertFalse(CashMovement.objects.exists())

    def test_bad_party_row_blocks_whole_file_and_type_cannot_be_reinterpreted(self):
        from customers.models import Customer, CustomerDebt
        preview = self.upload(b"name,phone,opening_debt_fcfa,opening_occurred_at,opening_reason\nA,,100,2026-01-01T10:00:00Z,Stock\nB,bad,100,2026-01-01T10:00:00Z,Stock\n")
        self.assertEqual(preview.status_code, 201)
        self.assertTrue(preview.data["has_errors"])
        self.assertEqual(self.confirm(preview.data).status_code, 409)
        self.assertEqual(self.confirm(preview.data, "suppliers").status_code, 404)
        self.assertFalse(Customer.objects.exists())
        self.assertFalse(CustomerDebt.objects.exists())

    def test_same_uuid_replay_and_new_confirmation_never_duplicate_parties_or_debts(self):
        from customers.models import Customer, CustomerDebt
        from common.models import Activity
        identifier = str(uuid.uuid4())
        source = b"name,opening_debt_fcfa,opening_occurred_at,opening_reason\nA,123,2026-01-01T10:00:00Z,Solde reel\n"
        preview = self.upload(source, identifier=identifier)
        self.assertEqual(preview.status_code, 201)
        self.assertEqual(self.upload(source, identifier=identifier).data, preview.data)
        self.assertEqual(self.upload(source, "suppliers", identifier).status_code, 409)
        body = {"operation_id": str(uuid.uuid4()), "source_sha256": preview.data["source_sha256"], "reason": "Confirmation"}
        original = self.confirm(preview.data, body=body)
        self.assertEqual(original.status_code, 200)
        self.assertEqual(self.confirm(preview.data, body=body).data, original.data)
        self.assertEqual(self.confirm(preview.data).status_code, 409)
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(CustomerDebt.objects.count(), 1)
        self.assertEqual(Activity.objects.filter(action="customers_import_applied").count(), 1)

    def test_late_uuid_collision_rolls_back_record_debt_and_ledger(self):
        from customers.models import Customer, CustomerDebt, CustomerLedgerEntry
        from common.models import Activity
        from catalogue.models import CatalogueImport
        source = b"name,opening_debt_fcfa,opening_occurred_at,opening_reason\nA,123,2026-01-01T10:00:00Z,Solde\nB,456,2026-01-01T10:00:00Z,Solde\n"
        preview = self.upload(source)
        self.assertEqual(preview.status_code, 201)
        self.assertFalse(preview.data["has_errors"])
        existing_id = uuid.uuid5(uuid.UUID(preview.data["id"]), "customer:3")
        Customer.objects.create(id=existing_id, shop=self.other, name="Existant")
        self.assertEqual(self.confirm(preview.data).status_code, 409)
        self.assertFalse(Customer.objects.filter(shop=self.shop).exists())
        self.assertFalse(CustomerDebt.objects.exists())
        self.assertFalse(CustomerLedgerEntry.objects.exists())
        self.assertFalse(Activity.objects.exists())
        self.assertIsNone(CatalogueImport.objects.get().applied_operation_id)

    def test_seller_foreign_shop_and_revoked_session_cannot_preview_or_apply(self):
        from identity.models import DeviceSession
        from customers.models import Customer
        from django.utils import timezone
        source = b"name,phone\nA,\n"
        preview = self.upload(source)
        self.assertEqual(preview.status_code, 201)
        identifier = preview.data["id"]
        self.assertEqual(self.client.get(self.url(suffix=identifier + "/", shop=self.other)).status_code, 404)
        self.authenticate(self.seller)
        self.assertEqual(self.upload(source).status_code, 403)
        self.assertEqual(self.client.get(self.url(suffix=identifier + "/")).status_code, 403)
        self.assertEqual(self.confirm(preview.data).status_code, 403)
        self.authenticate(self.owner)
        DeviceSession.objects.filter(installation__user=self.owner).update(revoked_at=timezone.now())
        self.assertEqual(self.confirm(preview.data).status_code, 401)
        self.assertFalse(Customer.objects.exists())

    def test_negative_fractional_or_undocumented_opening_fields_are_rejected(self):
        from customers.models import CustomerDebt
        for amount, date, reason, due in (("-1", "2026-01-01T00:00:00Z", "Solde", ""),
                                          ("1.5", "2026-01-01T00:00:00Z", "Solde", ""),
                                          ("100", "", "Solde", ""), ("100", "2026-01-01T00:00:00Z", "", ""),
                                          ("0", "", "", "2026-01-31")):
            with self.subTest(amount=amount, date=date, reason=reason):
                source = f"name,opening_debt_fcfa,opening_occurred_at,opening_reason,due_date\nA,{amount},{date},{reason},{due}\n".encode()
                preview = self.upload(source)
                self.assertTrue(preview.data["has_errors"], preview.data)
                self.assertEqual(self.confirm(preview.data).status_code, 409)
        self.assertFalse(CustomerDebt.objects.exists())

    def test_templates_are_typed_downloads_with_english_examples(self):
        for kind, label in (("customers", b"Sample customer"), ("suppliers", b"Sample supplier")):
            with self.subTest(kind=kind):
                response = self.client.get(self.url(kind, "template/"), HTTP_ACCEPT="text/csv", HTTP_ACCEPT_LANGUAGE="en")
                self.assertEqual(response.status_code, 200)
                self.assertIn(label, response.content)
                self.assertIn(kind + "-template.csv", response["Content-Disposition"])
                preview = self.upload(response.content, kind)
                self.assertEqual(preview.status_code, 201)
                self.assertFalse(preview.data["has_errors"], preview.data)

    def test_supplier_csv_keeps_debt_advance_and_credit_as_three_separate_openings(self):
        from purchasing.models import Supplier, SupplierCredit, SupplierDebt, SupplierPayment, PurchaseDocument
        from cash.models import CashMovement
        source = b"name,opening_debt_fcfa,opening_advance_fcfa,opening_credit_fcfa,opening_occurred_at,opening_reason\nF,7000,30000,5000,2026-01-01T10:00:00Z,Soldes confirmes\n"
        preview = self.upload(source, "suppliers")
        self.assertEqual(preview.status_code, 201)
        self.assertFalse(preview.data["has_errors"], preview.data)
        self.assertFalse(Supplier.objects.exists())
        body = {"operation_id": str(uuid.uuid4()), "source_sha256": preview.data["source_sha256"], "reason": "Reprise complète"}
        applied = self.confirm(preview.data, "suppliers", body)
        self.assertEqual(applied.status_code, 200, applied.content)
        self.assertEqual(len(applied.data["opening_debt_ids"]), 1)
        self.assertEqual(len(applied.data["opening_balance_ids"]), 2)
        self.assertEqual(SupplierDebt.objects.get().remaining_fcfa, 7000)
        self.assertEqual(dict(SupplierCredit.objects.values_list("kind", "remaining_fcfa")), {"opening_advance": 30000, "opening_credit": 5000})
        self.assertEqual(self.confirm(preview.data, "suppliers", body).data, applied.data)
        self.assertEqual(Supplier.objects.count(), 1)
        self.assertFalse(SupplierPayment.objects.exists())
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(PurchaseDocument.objects.exists())

    def test_supplier_csv_credit_without_debt_is_valid_but_invalid_balance_blocks_all_rows(self):
        from purchasing.models import Supplier
        source = b"name,opening_credit_fcfa,opening_occurred_at,opening_reason\nF,5000,2026-01-01T10:00:00Z,Avoir anterieur\n"
        preview = self.upload(source, "suppliers")
        self.assertFalse(preview.data["has_errors"], preview.data)
        for value in (b"-1", b"1.5"):
            with self.subTest(value=value):
                invalid = self.upload(source.replace(b"5000", value), "suppliers")
                self.assertTrue(invalid.data["has_errors"])
                self.assertEqual(self.confirm(invalid.data, "suppliers").status_code, 409)
        self.assertFalse(Supplier.objects.exists())

    def test_supplier_csv_late_opening_collision_rolls_back_all_three_balances(self):
        from common.models import Activity
        from purchasing.models import Supplier, SupplierDebt, SupplierCredit
        source = b"name,opening_debt_fcfa,opening_advance_fcfa,opening_credit_fcfa,opening_occurred_at,opening_reason\nF,7000,30000,5000,2026-01-01T10:00:00Z,Soldes confirmes\n"
        preview = self.upload(source, "suppliers")
        self.assertFalse(preview.data["has_errors"], preview.data)
        identifier = uuid.UUID(preview.data["id"])
        Activity.objects.create(id=uuid.uuid5(identifier, "opening_credit_operation:2"), shop=self.shop, actor=self.owner,
            action="other_operation", target_id=uuid.uuid4(), reason="Événement existant")
        self.assertEqual(self.confirm(preview.data, "suppliers").status_code, 409)
        self.assertFalse(Supplier.objects.exists())
        self.assertFalse(SupplierDebt.objects.exists())
        self.assertFalse(SupplierCredit.objects.exists())
        self.assertEqual(Activity.objects.count(), 1)

    def test_confirmation_uuid_cannot_be_reused_as_one_of_its_own_row_operations(self):
        from purchasing.models import Supplier, SupplierDebt, SupplierCredit
        from common.models import Activity
        source = b"name,opening_debt_fcfa,opening_advance_fcfa,opening_credit_fcfa,opening_occurred_at,opening_reason\nF,7000,30000,5000,2026-01-01T10:00:00Z,Soldes confirmes\n"
        preview = self.upload(source, "suppliers")
        self.assertFalse(preview.data["has_errors"])
        body = {"operation_id": str(uuid.uuid5(uuid.UUID(preview.data["id"]), "opening_credit_operation:2")),
                "source_sha256": preview.data["source_sha256"], "reason": "Collision à refuser"}
        response = self.confirm(preview.data, "suppliers", body)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(Supplier.objects.exists())
        self.assertFalse(SupplierDebt.objects.exists())
        self.assertFalse(SupplierCredit.objects.exists())
        self.assertFalse(Activity.objects.exists())
