"""PRD storage contracts: SQLite must never turn fractions into valid money."""
import uuid

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from catalogue.models import Category, Product
from cash.models import CashCount, CashMovement
from common.models import Activity
from customers.models import (Customer, CustomerCredit, CustomerDebt, CustomerLedgerEntry,
                              PaymentRegularization, RegularizationAllocation)
from identity.models import Installation, Membership, Shop, User
from sales.models import Sale, SaleLine, SalePayment
from purchasing.models import (PurchaseDocument, PurchaseLine, PurchaseReceipt, PurchaseReceiptLine,
                               Supplier, SupplierDebt, SupplierPayment, SupplierPaymentAllocation,
                               SupplierPaymentPart, SupplierReturn, SupplierReturnLine)
from stock.models import StockMovement


class PrdNumericModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(username="prd-numeric-owner")
        cls.shop = Shop.objects.create(owner=cls.owner, name="Boutique")
        cls.installation = Installation.objects.create(user=cls.owner)
        cls.product = Product.objects.create(
            shop=cls.shop, name="Riz", unit_kind="weight", unit_label="kg",
            sale_price_fcfa=249, purchase_price_fcfa=None,
        )
        cls.now = timezone.now()
        cls.customer = Customer.objects.create(shop=cls.shop, name="Client")

    def event(self, action="storage_verification"):
        return Activity.objects.create(
            shop=self.shop, actor=self.owner, action=action,
            target_id=uuid.uuid4(), reason="Contrôle du modèle PRD",
        )

    def reject_update(self, row, **changes):
        """Bypass ORM integer coercion and exercise the actual storage boundary."""
        quote = connection.ops.quote_name
        assignments = ", ".join(f"{quote(field)}=%s" for field in changes)
        with transaction.atomic():
            with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {quote(row._meta.db_table)} SET {assignments} WHERE id=%s",
                    [*changes.values(), row.pk.hex],
                )

    def sale(self):
        return Sale.objects.create(
            shop=self.shop, actor=self.owner, installation=self.installation,
            operation=self.event("sale_created"), occurred_at=self.now,
            reference_total_fcfa=1000, discount_total_fcfa=100, net_total_fcfa=900,
        )

    def sale_line(self):
        return SaleLine.objects.create(
            sale=self.sale(), product=self.product, position=0, product_version=1,
            product_name="Riz", unit_kind="weight", unit_label="kg",
            quantity_milli=1000, factor_milli=1000, base_quantity_milli=1000,
            unit_price_fcfa=1000, reference_fcfa=1000, own_discount_fcfa=50,
            global_discount_fcfa=50, net_fcfa=900,
        )

    def purchase(self):
        supplier = Supplier.objects.create(shop=self.shop, name="Fournisseur")
        invoice = PurchaseDocument.objects.create(
            shop=self.shop, supplier=supplier, operation=self.event(), installation=self.installation,
            kind="invoice", occurred_at=self.now, total_fcfa=1000,
        )
        line = PurchaseLine.objects.create(
            document=invoice, product=self.product, position=0, product_name="Riz", unit_label="kg",
            quantity_milli=1000, factor_milli=1000, base_quantity_milli=1000, unit_price_fcfa=1000, total_fcfa=1000,
        )
        debt = SupplierDebt.objects.create(
            shop=self.shop, supplier=supplier, document=invoice, operation=invoice.operation,
            installation=self.installation, occurred_at=self.now, amount_fcfa=1000, remaining_fcfa=1000,
        )
        return supplier, invoice, line, debt

    def test_us09_catalogue_money_refuses_fractional_fcfa_and_keeps_unknown_cost(self):
        for field in ("sale_price_fcfa", "purchase_price_fcfa"):
            for value in (0.5, "invalid"):
                with self.subTest(field=field, value=value):
                    self.reject_update(self.product, **{field: value})
        self.product.refresh_from_db()
        self.assertEqual(self.product.sale_price_fcfa, 249)
        self.assertIsNone(self.product.purchase_price_fcfa)
        Product.objects.filter(pk=self.product.pk).update(purchase_price_fcfa=0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.purchase_price_fcfa, 0)

    def test_us03_us09_concurrency_versions_are_positive_integers(self):
        membership = Membership.objects.create(shop=self.shop, user=self.owner)
        category = Category.objects.create(shop=self.shop, name="Alimentation")
        for row, field in ((membership, "version"), (category, "version"),
                           (self.product, "version"), (self.shop, "discount_policy_version")):
            for value in (0, 1.5, "invalid"):
                with self.subTest(model=row._meta.label, value=value):
                    self.reject_update(row, **{field: value})

    def test_us18_discount_cap_stores_only_whole_basis_points(self):
        for value in (-1, 0.5, 10001, "invalid"):
            with self.subTest(value=value):
                self.reject_update(self.shop, seller_discount_cap_basis_points=value)
        for value in (0, 10000):
            Shop.objects.filter(pk=self.shop.pk).update(seller_discount_cap_basis_points=value)
            self.shop.refresh_from_db()
            self.assertEqual(self.shop.seller_discount_cap_basis_points, value)

    def test_us17_sale_totals_refuse_balanced_fractional_money(self):
        sale = self.sale()
        self.reject_update(sale, reference_total_fcfa=1000.5, net_total_fcfa=900.5)
        self.reject_update(sale, discount_total_fcfa=100.5, net_total_fcfa=899.5)
        sale.refresh_from_db()
        self.assertEqual((sale.reference_total_fcfa, sale.discount_total_fcfa, sale.net_total_fcfa),
                         (1000, 100, 900))

    def test_us18_line_discounts_refuse_fractions_even_when_the_net_balances(self):
        line = self.sale_line()
        for changes in ({"own_discount_fcfa": 50.5, "global_discount_fcfa": 49.5},
                        {"unit_price_fcfa": 1000.5},
                        {"reference_fcfa": 1000.5, "net_fcfa": 900.5},
                        {"unit_cost_micros": 0.5}):
            with self.subTest(changes=changes):
                self.reject_update(line, **changes)

    def test_us22_split_payment_and_change_refuse_fractional_money(self):
        payment = SalePayment.objects.create(sale=self.sale(), position=0, method="cash",
                                             amount_fcfa=900, received_fcfa=1000, returned_fcfa=100)
        for changes in ({"amount_fcfa": 900.5, "received_fcfa": 1000.5},
                        {"received_fcfa": 1000.5, "returned_fcfa": 100.5},
                        {"position": 0.5}):
            with self.subTest(changes=changes):
                self.reject_update(payment, **changes)
        payment.refresh_from_db()
        self.assertEqual((payment.amount_fcfa, payment.received_fcfa, payment.returned_fcfa), (900, 1000, 100))

    def test_us10_historical_quantities_and_versions_refuse_fractional_storage(self):
        line = self.sale_line()
        for field in ("quantity_milli", "factor_milli", "base_quantity_milli", "product_version", "position"):
            with self.subTest(field=field):
                self.reject_update(line, **{field: 1.5})

    def test_us10_piece_sales_cannot_freeze_half_a_piece(self):
        line = self.sale_line()
        self.reject_update(line, unit_kind="piece", quantity_milli=500, base_quantity_milli=500)
        self.reject_update(line, unit_kind="unsupported")

    def test_us19_credit_sale_requires_an_identified_customer(self):
        self.reject_update(self.sale(), mode="credit")
        self.reject_update(self.sale(), mode="unsupported")

    def test_us31_cash_movements_keep_signed_whole_fcfa(self):
        movement = CashMovement.objects.create(
            shop=self.shop, actor=self.owner, installation=self.installation,
            operation=self.event(), method="cash", manual_kind="addition",
            amount_fcfa=1000, occurred_at=self.now, reason="Apport réel",
        )
        self.reject_update(movement, amount_fcfa=1000.5)
        self.reject_update(movement, amount_fcfa="invalid")
        CashMovement.objects.filter(pk=movement.pk).update(manual_kind="withdrawal", amount_fcfa=-1000)
        movement.refresh_from_db()
        self.assertEqual(movement.amount_fcfa, -1000)

    def test_us32_physical_cash_observation_cannot_contain_half_a_franc(self):
        count = CashCount.objects.create(
            shop=self.shop, actor=self.owner, installation=self.installation,
            operation=self.event(), method="cash", physical_fcfa=48000,
            expected_at_creation_fcfa=50000, counted_at=self.now, reason="Comptage",
        )
        for value in (48000.5, "invalid"):
            with self.subTest(value=value):
                self.reject_update(count, physical_fcfa=value)
        count.refresh_from_db()
        self.assertEqual((count.physical_fcfa, count.expected_at_creation_fcfa), (48000, 50000))

    def test_us20_customer_correction_version_is_a_positive_integer(self):
        for value in (0, 1.5, "invalid"):
            with self.subTest(value=value):
                self.reject_update(self.customer, version=value)

    def test_us23_credit_refund_and_allocation_amounts_are_integral(self):
        credit = CustomerCredit.objects.create(
            shop=self.shop, customer=self.customer, operation=self.event(),
            amount_fcfa=1000, remaining_fcfa=1000, occurred_at=self.now,
        )
        refund = PaymentRegularization.objects.create(
            credit=credit, operation=self.event(), installation=self.installation,
            kind="refund", method="cash", amount_fcfa=100, after_unallocated_fcfa=900,
            occurred_at=self.now,
        )
        debt = CustomerDebt.objects.create(
            shop=self.shop, customer=self.customer, actor=self.owner, installation=self.installation,
            operation=self.event(), amount_fcfa=1000, remaining_fcfa=1000, occurred_at=self.now,
        )
        decision = PaymentRegularization.objects.create(
            credit=credit, operation=self.event(), installation=self.installation,
            kind="allocation", amount_fcfa=100, after_unallocated_fcfa=800, occurred_at=self.now,
        )
        allocation = RegularizationAllocation.objects.create(
            regularization=decision, debt=debt, position=0, amount_fcfa=100,
        )
        self.reject_update(credit, amount_fcfa=0.5, remaining_fcfa=0.5)
        for row, fields in ((credit, ("amount_fcfa", "remaining_fcfa")),
                            (refund, ("amount_fcfa", "after_unallocated_fcfa")),
                            (allocation, ("amount_fcfa", "position"))):
            for field in fields:
                with self.subTest(model=row._meta.label, field=field):
                    value = 1000.5 if row is credit and field == "amount_fcfa" else 0.5
                    self.reject_update(row, **{field: value})

    def test_us24_customer_statement_effects_refuse_fractional_money_and_unknown_states(self):
        entry = CustomerLedgerEntry.objects.create(
            shop=self.shop, customer=self.customer, operation=self.event(), source_id=uuid.uuid4(),
            kind="opening_debt", occurred_at=self.now, recorded_at=self.now,
            delta_fcfa=1000, debt_delta_fcfa=1000, credit_delta_fcfa=0,
        )
        for field in ("delta_fcfa", "debt_delta_fcfa", "credit_delta_fcfa", "sequence"):
            with self.subTest(field=field):
                self.reject_update(entry, **{field: 0.5})
        self.reject_update(entry, state="unknown")
        CustomerLedgerEntry.objects.filter(pk=entry.pk).update(delta_fcfa=-1000, debt_delta_fcfa=-1000)
        entry.refresh_from_db()
        self.assertEqual(entry.delta_fcfa, -1000)

    def test_us27_supplier_versions_invoice_and_debt_refuse_fractional_values(self):
        supplier, invoice, _line, debt = self.purchase()
        for value in (0, 1.5, "invalid"):
            with self.subTest(version=value):
                self.reject_update(supplier, version=value)
        for row, changes in ((invoice, {"total_fcfa": 1000.5}),
                             (debt, {"amount_fcfa": 1000.5}),
                             (debt, {"remaining_fcfa": 0.5})):
            with self.subTest(model=row._meta.label, changes=changes):
                self.reject_update(row, **changes)

    def test_us28_invoice_and_received_quantities_are_exact_milliunits(self):
        _supplier, invoice, line, _debt = self.purchase()
        receipt = PurchaseReceipt.objects.create(document=invoice, operation=self.event(),
                                                installation=self.installation, occurred_at=self.now)
        movement = StockMovement.objects.create(product=self.product, actor=self.owner, operation=receipt.operation,
                                                kind="purchase_receipt", quantity_milli=1000, occurred_at=self.now)
        received = PurchaseReceiptLine.objects.create(receipt=receipt, line=line, movement=movement,
                                                      quantity_milli=1000, base_quantity_milli=1000)
        for field in ("quantity_milli", "base_quantity_milli"):
            with self.subTest(received_field=field):
                self.reject_update(received, **{field: 1000.5})
        for changes in ({"position": 0.5}, {"unit_price_fcfa": 1000.5, "total_fcfa": 1000.5},
                        {"quantity_milli": 1000.5, "base_quantity_milli": 1000.5, "total_fcfa": 1000.5},
                        {"factor_milli": 1000.5, "base_quantity_milli": 1000.5}):
            with self.subTest(line_changes=changes):
                self.reject_update(line, **changes)

    def test_us29_supplier_payments_and_allocations_store_whole_fcfa(self):
        supplier, _invoice, _line, debt = self.purchase()
        payment = SupplierPayment.objects.create(shop=self.shop, supplier=supplier, operation=self.event(),
            installation=self.installation, occurred_at=self.now, amount_fcfa=1000, unallocated_fcfa=1000)
        part = SupplierPaymentPart.objects.create(payment=payment, position=0, method="cash", amount_fcfa=1000)
        allocation = SupplierPaymentAllocation.objects.create(payment=payment, debt=debt,
            operation=self.event(), position=0, amount_fcfa=100)
        for changes in ({"amount_fcfa": 1000.5}, {"unallocated_fcfa": 0.5}):
            with self.subTest(payment_changes=changes):
                self.reject_update(payment, **changes)
        for row in (part, allocation):
            for field in ("amount_fcfa", "position"):
                with self.subTest(model=row._meta.label, field=field):
                    self.reject_update(row, **{field: 0.5})

    def test_us30_supplier_physical_return_does_not_store_fractional_milliunits(self):
        _supplier, invoice, line, _debt = self.purchase()
        returned = SupplierReturn.objects.create(document=invoice, operation=self.event(),
                                                 installation=self.installation, occurred_at=self.now)
        movement = StockMovement.objects.create(product=self.product, actor=self.owner, operation=returned.operation,
                                                kind="supplier_return", quantity_milli=-1000, occurred_at=self.now)
        departure = SupplierReturnLine.objects.create(supplier_return=returned, line=line, movement=movement,
                                                      quantity_milli=1000, base_quantity_milli=1000)
        for field in ("quantity_milli", "base_quantity_milli"):
            with self.subTest(field=field):
                self.reject_update(departure, **{field: 1000.5})
