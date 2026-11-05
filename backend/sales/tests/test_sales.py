"""US-17/18/36: durable cash sales, server pricing and idempotent stock effects."""
from datetime import timedelta
import uuid
from unittest.mock import patch

from django.db.models import Sum
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from cash.models import CashMovement
from identity.models import DeviceSession, Installation, Membership, Shop, User
from catalogue.models import Product, ProductPack
from sales.models import Sale, SaleLine, SalePayment
from stock.models import StockMovement


class CashSaleTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="sale-owner")
        self.seller = User.objects.create_user(username="sale-seller")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        self.other = Shop.objects.create(name="B", owner=self.owner)
        self.member = Membership.objects.create(shop=self.shop, user=self.seller)
        self.product = Product.objects.create(shop=self.shop, name="Bouteille", unit_kind="piece", unit_label="pièce", sale_price_fcfa=5000)
        self.weight = Product.objects.create(shop=self.shop, name="Farine", unit_kind="weight", unit_label="kg", sale_price_fcfa=249)
        self.foreign = Product.objects.create(shop=self.other, name="Foreign", unit_kind="piece", unit_label="pièce", sale_price_fcfa=5000)
        opening = Activity.objects.create(shop=self.shop, actor=self.owner, action="product_created", target_id=self.product.id, reason="Reprise")
        StockMovement.objects.create(product=self.product, actor=self.owner, operation=opening, kind="opening", quantity_milli=10000,
                                     unit_cost_micros=2000000000, occurred_at=timezone.now(), reason="Reprise")
        self.client = APIClient()
        self.device = self.authenticate(self.seller)

    def authenticate(self, user):
        device = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return device

    def url(self, sale_id=None, shop=None):
        base = f"/api/v1/shops/{(shop or self.shop).id}/sales/"
        return base if sale_id is None else base + str(sale_id) + "/"

    def payload(self, **changes):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1,
                           "quantity_milli": 2000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 4000},
                             {"id": str(uuid.uuid4()), "method": "orange", "amount_fcfa": 6000}], **changes}

    def create(self, data=None, shop=None):
        return self.client.post(self.url(shop=shop), data or self.payload(), format="json")

    def stock(self, product=None):
        return StockMovement.objects.filter(product=product or self.product).aggregate(total=Sum("quantity_milli"))["total"] or 0

    def test_sale_commits_lines_payments_stock_and_audit_once_with_original_receipt(self):
        data = self.payload()
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["net_total_fcfa"], 10000)
        self.assertEqual(response.data["installation_id"], str(self.device.id))
        self.assertEqual(self.stock(), 8000)
        self.assertEqual([payment["amount_fcfa"] for payment in response.data["payments"]], [4000, 6000])
        self.assertEqual(response.data["lines"][0]["product_name"], "Bouteille")
        self.assertEqual(response.data["lines"][0]["unit_price_fcfa"], 5000)
        self.product.sale_price_fcfa = 7000
        self.product.name = "Nouveau nom"
        self.product.version = 2
        self.product.save()
        retry = self.create(data)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, response.data)
        self.assertEqual(self.client.get(self.url(data["id"])).data, response.data)
        self.assertEqual(self.stock(), 8000)
        self.assertEqual(Activity.objects.filter(action="sale_created").count(), 1)
        self.assertEqual(StockMovement.objects.filter(kind="sale").count(), 1)

    def test_multiline_rounding_and_pack_factor_are_frozen_and_share_base_stock(self):
        pack = ProductPack.objects.create(product=self.product, name="Carton", base_quantity_milli=24000, sale_price_fcfa=4800)
        data = self.payload(lines=[
            {"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1,
             "pack_id": str(pack.id), "quantity_milli": 1000},
            {"id": str(uuid.uuid4()), "product_id": str(self.weight.id), "product_version": 1, "quantity_milli": 500},
        ], payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 4925}])
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["net_total_fcfa"], 4925)
        self.assertEqual([line["net_fcfa"] for line in response.data["lines"]], [4800, 125])
        self.assertEqual(response.data["lines"][0]["base_quantity_milli"], 24000)
        self.assertEqual(response.data["lines"][0]["factor_milli"], 24000)
        self.assertEqual(self.stock(), -14000)
        self.assertEqual(self.stock(self.weight), -500)
        self.assertTrue(response.data["stock_requires_regularization"])
        self.assertEqual(StockMovement.objects.filter(kind="sale").count(), 2)
        pack.base_quantity_milli = 12000
        pack.sale_price_fcfa = 6000
        pack.save()
        self.assertEqual(self.client.get(self.url(data["id"])).data["lines"][0]["factor_milli"], 24000)

    def test_two_real_sales_can_make_stock_negative_without_erasing_payment(self):
        data = self.payload(lines=[{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1,
                                   "quantity_milli": 10000}],
                            payments=[{"id": str(uuid.uuid4()), "method": "mtn", "amount_fcfa": 50000}])
        self.assertEqual(self.create(data).status_code, 201)
        second = self.create()
        self.assertEqual(second.status_code, 201)
        self.assertTrue(second.data["stock_requires_regularization"])
        self.assertEqual(self.stock(), -2000)
        listed = self.client.get(self.url())
        self.assertEqual(listed.data["count"], 2)
        self.assertEqual(sum(item["net_total_fcfa"] for item in listed.data["results"]), 60000)

    def test_payload_collision_partial_invalid_basket_and_duplicate_ids_have_no_partial_effect(self):
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        self.assertEqual(self.create({**data, "payments": []}).status_code, 409)
        bad = self.payload()
        bad["lines"].append({"id": str(uuid.uuid4()), "product_id": str(self.foreign.id), "product_version": 1, "quantity_milli": 1000})
        self.assertEqual(self.create(bad).status_code, 404)
        duplicate = self.payload()
        duplicate["lines"].append(dict(duplicate["lines"][0]))
        self.assertEqual(self.create(duplicate).status_code, 400)
        duplicate = self.payload()
        duplicate["payments"][1]["id"] = duplicate["payments"][0]["id"]
        self.assertEqual(self.create(duplicate).status_code, 400)
        self.assertEqual(self.stock(), 8000)
        self.assertEqual(Activity.objects.filter(action="sale_created").count(), 1)

    def test_default_discount_cap_cannot_be_bypassed_by_seller_and_stale_price_is_not_used(self):
        data = self.payload(global_discount_fcfa=1)
        self.assertEqual(self.create(data).status_code, 400)
        self.assertEqual(self.create(self.payload(seller_cap_basis_points=10000)).status_code, 400)
        data = self.payload()
        data["lines"][0]["product_version"] = 2
        self.assertEqual(self.create(data).status_code, 409)
        self.assertEqual(self.stock(), 10000)
        self.assertFalse(Activity.objects.filter(action="sale_created").exists())

    def test_wrong_payment_total_archived_product_invalid_quantity_and_client_fields_are_rejected(self):
        bad_cases = [self.payload(payments=[]), self.payload(occurred_at="2026-01-01T00:00:00Z"),
                     self.payload(lines=[]), self.payload(net_total_fcfa=1)]
        for quantity in [True, 1.5, -1000, 0, 1500, 9223372036854775807]:
            data = self.payload()
            data["lines"][0]["quantity_milli"] = quantity
            bad_cases.append(data)
        for data in bad_cases:
            with self.subTest(data=data):
                self.assertEqual(self.create(data).status_code, 400)
        self.product.is_active = False
        self.product.save()
        self.assertEqual(self.create().status_code, 409)
        self.assertEqual(self.stock(), 10000)
        self.assertFalse(Activity.objects.filter(action="sale_created").exists())

    def test_read_and_write_are_shop_scoped_and_retries_check_current_membership(self):
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        self.assertEqual(self.client.get(self.url(data["id"], self.other)).status_code, 404)
        self.assertEqual(self.create(shop=self.other).status_code, 404)
        self.member.is_active = False
        self.member.save()
        self.assertEqual(self.create(data).status_code, 404)
        self.assertEqual(self.client.get(self.url(data["id"])).status_code, 404)
        self.assertEqual(self.stock(), 8000)

    def test_other_named_method_and_external_reference_are_persisted_without_provider_call(self):
        data = self.payload(payments=[{"id": str(uuid.uuid4()), "method": "other", "other_name": "Bon déclaré",
                                      "amount_fcfa": 10000, "external_reference": "REFERENCE-TEST"}])
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["payments"][0]["other_name"], "Bon déclaré")
        self.assertEqual(response.data["payments"][0]["external_reference"], "REFERENCE-TEST")
        self.assertNotIn("unit_cost_micros", response.data["lines"][0])
        data = self.payload(payments=[{"id": str(uuid.uuid4()), "method": "other", "amount_fcfa": 10000}])
        self.assertEqual(self.create(data).status_code, 400)

    def test_cash_is_ventilated_by_actual_method_once_and_unknown_cost_is_not_zero(self):
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        self.assertEqual(self.create(data).status_code, 200)
        self.assertEqual(CashMovement.objects.filter(shop=self.shop, method="cash").aggregate(total=Sum("amount_fcfa"))["total"], 4000)
        self.assertEqual(CashMovement.objects.filter(shop=self.shop, method="orange").aggregate(total=Sum("amount_fcfa"))["total"], 6000)
        self.assertEqual(CashMovement.objects.count(), 2)
        self.assertEqual(SalePayment.objects.count(), 2)
        line = SaleLine.objects.get(sale_id=data["id"])
        self.assertIsNone(line.unit_cost_micros)
        self.assertEqual(CashMovement.objects.get(method="cash").installation_id, self.device.id)

    def test_seller_discounts_use_shop_cap_and_cumulative_line_discount(self):
        self.shop.seller_discount_cap_basis_points = 500
        self.shop.save()
        data = self.payload(global_discount_fcfa=400, payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 9500}])
        data["lines"][0]["own_discount_fcfa"] = 100
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["discount_total_fcfa"], 500)
        self.assertEqual(response.data["lines"][0]["global_discount_fcfa"], 400)
        data = self.payload(global_discount_fcfa=401, payments=[{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 9499}])
        data["lines"][0]["own_discount_fcfa"] = 100
        self.assertEqual(self.create(data).status_code, 400)
        self.assertEqual(Sale.objects.count(), 1)

    def test_zero_priced_sale_needs_no_fictitious_payment_and_payment_uuid_collision_rolls_back(self):
        self.product.sale_price_fcfa = 0
        self.product.save()
        data = self.payload(payments=[])
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["net_total_fcfa"], 0)
        self.assertFalse(CashMovement.objects.exists())
        self.product.sale_price_fcfa = 5000
        self.product.save()
        paid = self.payload()
        self.assertEqual(self.create(paid).status_code, 201)
        collision = self.payload()
        collision["payments"][0]["id"] = paid["payments"][0]["id"]
        self.assertEqual(self.create(collision).status_code, 409)
        self.assertEqual(Sale.objects.count(), 2)
        self.assertEqual(self.stock(), 6000)

    def test_total_stock_overflow_rolls_back_sale_lines_and_audit(self):
        self.product.sale_price_fcfa = 0
        self.product.save()
        large = 9223372036854775000
        data = self.payload(lines=[
            {"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1, "quantity_milli": large},
            {"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1, "quantity_milli": 1000},
        ], payments=[])
        self.assertEqual(self.create(data).status_code, 400)
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(SaleLine.objects.exists())
        self.assertFalse(Activity.objects.filter(action="sale_created").exists())
        self.assertEqual(self.stock(), 10000)

    def test_stock_underflow_is_rejected_before_any_second_sale_effect(self):
        self.product.sale_price_fcfa = 0
        self.product.save()
        first = self.payload(lines=[{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1,
                                     "quantity_milli": 9223372036854775000}], payments=[])
        self.assertEqual(self.create(first).status_code, 201)
        second = self.payload(lines=[{"id": str(uuid.uuid4()), "product_id": str(self.product.id), "product_version": 1,
                                      "quantity_milli": 20000}], payments=[])
        self.assertEqual(self.create(second).status_code, 400)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(SaleLine.objects.count(), 1)
        self.assertEqual(self.stock(), -9223372036854765000)

    def test_revocation_after_authentication_before_transaction_prevents_sale(self):
        from sales.services.sale_services import create_sale
        def revoke_then_create(*args, **kwargs):
            Installation.objects.filter(id=self.device.id).update(revoked_at=timezone.now())
            return create_sale(*args, **kwargs)
        with patch("sales.api.sales.create_sale", side_effect=revoke_then_create):
            response = self.create()
        self.assertEqual(response.status_code, 401)
        self.assertFalse(Sale.objects.exists())
        self.assertEqual(self.stock(), 10000)

    def test_session_revoked_after_authentication_also_prevents_original_receipt_retry(self):
        from sales.services.sale_services import create_sale
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        def revoke_then_create(*args, **kwargs):
            DeviceSession.objects.filter(installation=self.device).update(revoked_at=timezone.now())
            return create_sale(*args, **kwargs)
        with patch("sales.api.sales.create_sale", side_effect=revoke_then_create):
            response = self.create(data)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(self.stock(), 8000)

    def test_sale_uses_exact_stock_when_valid_movements_sum_above_sqlite_int64(self):
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="purchase_received", target_id=self.product.id, reason="Réception physique")
        StockMovement.objects.create(product=self.product, actor=self.owner, operation=event, kind="purchase_receipt",
            quantity_milli=9223372036854775000, occurred_at=timezone.now(), reason="Grand stock exact")
        created = self.create()
        self.assertEqual(created.status_code, 201, created.content)
        self.assertFalse(created.data["stock_requires_regularization"])
