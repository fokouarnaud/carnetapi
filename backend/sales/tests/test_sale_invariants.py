"""Financial totals must remain exact even when a write bypasses the API."""
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from common.models import Activity
from identity.models import Installation, Shop, User
from catalogue.models import Product
from sales.models import Sale, SaleLine


class SaleInvariantTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="sale-invariant")
        shop = Shop.objects.create(name="Invariant", owner=user)
        device = Installation.objects.create(user=user)
        product = Product.objects.create(shop=shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=1000)
        audit = Activity.objects.create(shop=shop, actor=user, action="sale_created", target_id=product.id, reason="Test")
        self.sale = Sale.objects.create(shop=shop, actor=user, installation=device, operation=audit, occurred_at=timezone.now(),
                                        reference_total_fcfa=1000, discount_total_fcfa=100, net_total_fcfa=900)
        self.line = SaleLine.objects.create(sale=self.sale, product=product, position=0, product_version=1, product_name="Produit",
                                            unit_kind="piece", unit_label="pièce", quantity_milli=1000, factor_milli=1000,
                                            base_quantity_milli=1000, unit_price_fcfa=1000, reference_fcfa=1000,
                                            own_discount_fcfa=50, global_discount_fcfa=50, net_fcfa=900)

    def test_database_rejects_sale_total_that_creates_or_loses_a_franc(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Sale.objects.filter(id=self.sale.id).update(net_total_fcfa=901)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.net_total_fcfa, 900)

    def test_database_rejects_inconsistent_line_net_or_discounts(self):
        for changes in [{"net_fcfa": 899}, {"global_discount_fcfa": 1001, "net_fcfa": 0}]:
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                SaleLine.objects.filter(id=self.line.id).update(**changes)
        self.line.refresh_from_db()
        self.assertEqual(self.line.net_fcfa, 900)

    def test_database_rejects_nonpositive_physical_quantity(self):
        for field in ["quantity_milli", "factor_milli", "base_quantity_milli"]:
            with self.subTest(field=field), self.assertRaises(IntegrityError), transaction.atomic():
                SaleLine.objects.filter(id=self.line.id).update(**{field: 0})
