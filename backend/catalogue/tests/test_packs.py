from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from identity.models import DeviceSession, Installation, Membership, Shop, ShopDeviceAccess, User
from catalogue.models import Product
from stock.models import StockMovement


class PackTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="pack-owner")
        self.seller = User.objects.create_user(username="pack-seller")
        self.shop = Shop.objects.create(name="Boutique", owner=self.owner)
        self.other = Shop.objects.create(name="Autre", owner=self.owner)
        self.membership = Membership.objects.create(shop=self.shop, user=self.seller)
        self.product = Product.objects.create(shop=self.shop, name="Bouteille", unit_kind="piece", unit_label="bouteille", sale_price_fcfa=250)
        self.weight = Product.objects.create(shop=self.shop, name="Farine", unit_kind="weight", unit_label="kg", sale_price_fcfa=1000)
        self.foreign = Product.objects.create(shop=self.other, name="Autre", unit_kind="piece", unit_label="pièce", sale_price_fcfa=200)
        self.client = APIClient()
        self.authenticate(self.owner)

    def authenticate(self, user):
        device = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=1))
        access = AccessToken.for_user(user)
        access["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return device

    def url(self, product=None, shop=None):
        return f"/api/v1/shops/{(shop or self.shop).id}/products/{(product or self.product).id}/packs/"

    def payload(self, **changes):
        body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Carton",
                "base_quantity_milli": 24000, "sale_price_fcfa": 4800}
        body.update(changes)
        return body

    def create(self, body=None, product=None, shop=None):
        return self.client.post(self.url(product, shop), body or self.payload(), format="json")

    def test_multiple_custom_packs_keep_independent_prices_and_do_not_create_stock(self):
        carton = self.create()
        self.assertEqual(carton.status_code, 201)
        self.assertEqual(carton.data["base_quantity_milli"], 24000)
        self.assertEqual(carton.data["sale_price_fcfa"], 4800)
        paquet = self.create(self.payload(name="Paquet de six", base_quantity_milli=6000, sale_price_fcfa=1400))
        self.assertEqual(paquet.status_code, 201)
        listed = self.client.get(self.url())
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data["count"], 2)
        self.assertEqual({p["name"] for p in listed.data["results"]}, {"Carton", "Paquet de six"})
        self.assertEqual(StockMovement.objects.count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.sale_price_fcfa, 250)

    def test_idempotent_retry_and_conflicts_keep_one_audit(self):
        body = self.payload()
        response = self.create(body)
        self.assertEqual(response.status_code, 201)
        retry = self.create(body)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, response.data)
        self.assertEqual(self.create({**body, "base_quantity_milli": 12000}).status_code, 409)
        self.assertEqual(self.create({**body, "operation_id": str(uuid.uuid4())}).status_code, 409)
        audit = Activity.objects.get(id=body["operation_id"])
        self.assertEqual(audit.target_id, uuid.UUID(body["id"]))
        self.assertEqual(Activity.objects.count(), 1)

    def test_seller_read_allowed_but_write_denied_including_retry(self):
        body = self.payload()
        self.assertEqual(self.create(body).status_code, 201)
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.url()).data["count"], 1)
        self.assertEqual(self.create().status_code, 403)
        self.assertEqual(self.create(body).status_code, 403)

    def test_manager_can_create_but_membership_removal_blocks_listing(self):
        self.membership.role = Membership.Role.MANAGER
        self.membership.save()
        self.authenticate(self.seller)
        self.assertEqual(self.create().status_code, 201)
        self.membership.is_active = False
        self.membership.save()
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_cross_shop_product_and_revoked_installation_cannot_access(self):
        device = self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.url(self.foreign)).status_code, 404)
        self.assertEqual(self.client.get(self.url(self.foreign, self.other)).status_code, 404)
        ShopDeviceAccess.objects.create(shop=self.shop, installation=device, revoked_at=timezone.now())
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        self.assertEqual(self.create().status_code, 404)

    def test_piece_factor_and_integer_price_validation_is_atomic(self):
        for changes in [{"base_quantity_milli": 0}, {"base_quantity_milli": -1000},
                        {"base_quantity_milli": 1500}, {"base_quantity_milli": 1000.5},
                        {"base_quantity_milli": True}, {"sale_price_fcfa": 1.5},
                        {"sale_price_fcfa": -1}, {"name": " "}, {"shop_id": str(self.other.id)}]:
            with self.subTest(changes=changes):
                self.assertEqual(self.create(self.payload(**changes)).status_code, 400)
        self.assertEqual(self.client.get(self.url()).data["count"], 0)
        self.assertEqual(Activity.objects.count(), 0)

    def test_weight_pack_accepts_three_decimal_base_quantity(self):
        response = self.create(self.payload(name="Sachet", base_quantity_milli=1250, sale_price_fcfa=1100), self.weight)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["base_quantity_milli"], 1250)
        self.assertEqual(response.data["product_id"], str(self.weight.id))

    def test_archived_product_cannot_receive_new_pack(self):
        self.product.is_active = False
        self.product.save()
        self.assertEqual(self.create().status_code, 409)
        self.assertEqual(Activity.objects.count(), 0)

    def test_pack_list_is_product_filtered_and_paginated(self):
        for index in range(3):
            self.assertEqual(self.create(self.payload(name=f"Pack {index}")).status_code, 201)
        self.assertEqual(self.create(product=self.weight).status_code, 201)
        first = self.client.get(self.url(), {"page_size": 2})
        self.assertEqual(first.data["count"], 3)
        self.assertEqual(len(first.data["results"]), 2)
        second = self.client.get(first.data["next"])
        self.assertEqual(len(second.data["results"]), 1)
        self.assertFalse({p["id"] for p in first.data["results"]} & {p["id"] for p in second.data["results"]})

    def test_operation_cannot_be_reused_for_another_product(self):
        body = self.payload()
        self.assertEqual(self.create(body).status_code, 201)
        self.assertEqual(self.create(body, product=self.weight).status_code, 409)
        self.assertEqual(self.client.get(self.url(self.weight)).data["count"], 0)

    def test_retry_returns_original_receipt_after_product_archiving(self):
        body = self.payload()
        original = self.create(body)
        self.assertEqual(original.status_code, 201)
        self.product.is_active = False
        self.product.save()
        retry = self.create(body)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, original.data)
        self.assertEqual(self.create().status_code, 409)
