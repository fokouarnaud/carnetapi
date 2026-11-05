from datetime import timedelta
from decimal import Decimal
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from identity.models import DeviceSession, Installation, Membership, Shop, ShopDeviceAccess, User
from stock.models import StockMovement


class CatalogueTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="catalogue-owner")
        self.seller = User.objects.create_user(username="catalogue-seller")
        self.shop = Shop.objects.create(name="Boutique", owner=self.owner)
        self.other = Shop.objects.create(name="Autre", owner=self.owner)
        self.membership = Membership.objects.create(shop=self.shop, user=self.seller)
        self.client = APIClient()
        self.authenticate(self.owner)

    def authenticate(self, user):
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return installation

    def payload(self, **changes):
        data = {
            "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "name": "Savon", "unit_kind": "piece", "unit_label": "pièce",
            "sale_price_fcfa": 1000, "purchase_price_fcfa": 600,
            "opening": {"quantity_milli": 10000, "unit_cost_fcfa": "600.000000",
                        "occurred_at": "2026-01-01T10:00:00Z", "reason": "Reprise du stock"},
        }
        data.update(changes)
        return data

    def url(self, shop=None, product=None):
        base = f"/api/v1/shops/{(shop or self.shop).id}/products/"
        return base if product is None else f"{base}{product}/"

    def create(self, data=None, shop=None):
        return self.client.post(self.url(shop), data or self.payload(), format="json")

    def test_create_opening_stock_and_retry_without_double_stock(self):
        data = self.payload()
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["id"], data["id"])
        self.assertEqual(response.data["stock_validated_milli"], 10000)
        self.assertEqual(response.data["purchase_price_fcfa"], 600)
        retry = self.create(data)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, response.data)
        detail = self.client.get(self.url(product=data["id"]))
        self.assertEqual(detail.data["stock_validated_milli"], 10000)
        audit = Activity.objects.get(id=data["operation_id"])
        self.assertEqual(audit.target_id, uuid.UUID(data["id"]))
        self.assertEqual(audit.details["opening"]["quantity_milli"], 10000)
        self.assertEqual(Activity.objects.filter(target_id=data["id"]).count(), 1)

    def test_retry_conflict_and_product_uuid_collision_do_not_mutate(self):
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        self.assertEqual(self.create({**data, "sale_price_fcfa": 900}).status_code, 409)
        self.assertEqual(self.create({**data, "operation_id": str(uuid.uuid4())}).status_code, 409)
        self.assertEqual(self.client.get(self.url(product=data["id"])).data["sale_price_fcfa"], 1000)

    def test_seller_reads_without_cost_and_permission_is_checked_on_every_request(self):
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        self.authenticate(self.seller)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertNotIn("purchase_price_fcfa", response.data["results"][0])
        self.assertNotIn("opening", response.data["results"][0])
        self.assertNotIn("purchase_price_fcfa", self.client.get(self.url(product=data["id"])).data)
        self.assertEqual(self.create().status_code, 403)
        self.membership.can_read_cost = True
        self.membership.save()
        self.assertEqual(self.client.get(self.url(product=data["id"])).data["purchase_price_fcfa"], 600)
        self.membership.can_read_cost = False
        self.membership.save()
        self.assertNotIn("purchase_price_fcfa", self.client.get(self.url(product=data["id"])).data)

    def test_cross_shop_details_and_revoked_installation_are_hidden(self):
        product = self.payload()
        self.assertEqual(self.create(product, self.other).status_code, 201)
        installation = self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.url(self.other)).status_code, 404)
        self.assertEqual(self.client.get(self.url(product=product["id"])).status_code, 404)
        ShopDeviceAccess.objects.create(shop=self.shop, installation=installation, revoked_at=timezone.now())
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        self.assertEqual(self.create().status_code, 404)

    def test_integer_money_and_quantity_validation_is_atomic(self):
        for field, invalid in [("sale_price_fcfa", 1000.5), ("sale_price_fcfa", True),
                               ("purchase_price_fcfa", -1)]:
            with self.subTest(field=field, invalid=invalid):
                self.assertEqual(self.create(self.payload(**{field: invalid})).status_code, 400)
        for invalid in [1, -1000, 1000.5, True]:
            with self.subTest(quantity=invalid):
                data = self.payload()
                data["opening"]["quantity_milli"] = invalid
                self.assertEqual(self.create(data).status_code, 400)
        self.assertEqual(self.client.get(self.url()).data["count"], 0)
        self.assertEqual(Activity.objects.count(), 0)

    def test_fractional_weight_and_unknown_initial_cost_are_preserved(self):
        data = self.payload(unit_kind="weight", unit_label="kg")
        data["opening"].update(quantity_milli=1250, unit_cost_fcfa=None)
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["stock_validated_milli"], 1250)
        audit = Activity.objects.get(id=data["operation_id"])
        self.assertIsNone(audit.details["opening"]["unit_cost_fcfa"])

    def test_missing_opening_starts_at_zero_and_manager_can_create(self):
        self.membership.role = Membership.Role.MANAGER
        self.membership.save()
        self.authenticate(self.seller)
        data = self.payload(opening=None)
        response = self.create(data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["stock_validated_milli"], 0)

    def test_opening_requires_reason_and_nonfuture_time(self):
        for changes in [{"reason": " "}, {"occurred_at": "2100-01-01T00:00:00Z"}]:
            data = self.payload()
            data["opening"].update(changes)
            self.assertEqual(self.create(data).status_code, 400)
        self.assertEqual(Activity.objects.count(), 0)

    def test_pagination_is_bounded_and_shop_filtered(self):
        for index in range(3):
            self.assertEqual(self.create(self.payload(name=f"Produit {index}")).status_code, 201)
        self.assertEqual(self.create(shop=self.other).status_code, 201)
        response = self.client.get(self.url(), {"page_size": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(len(response.data["results"]), 2)
        second = self.client.get(response.data["next"])
        self.assertEqual(len(second.data["results"]), 1)
        self.assertFalse({p["id"] for p in response.data["results"]} & {p["id"] for p in second.data["results"]})

    def test_initial_cost_keeps_all_six_decimals_after_sqlite_round_trip(self):
        data = self.payload()
        data["opening"]["unit_cost_fcfa"] = "1234567890.123456"
        self.assertEqual(self.create(data).status_code, 201)
        movement = StockMovement.objects.get(product_id=data["id"])
        self.assertEqual(movement.unit_cost_fcfa, Decimal("1234567890.123456"))

    def test_retry_after_rights_withdrawal_is_refused_even_for_existing_operation(self):
        self.membership.role = Membership.Role.MANAGER
        self.membership.save()
        self.authenticate(self.seller)
        data = self.payload()
        self.assertEqual(self.create(data).status_code, 201)
        self.membership.role = Membership.Role.SELLER
        self.membership.save()
        self.assertEqual(self.create(data).status_code, 403)

    def test_initial_cost_rejects_float_and_overflow_without_creating_product(self):
        for cost in [600.5, True, "-0.000001", "9223372036854.775808", "1.0000001"]:
            with self.subTest(cost=cost):
                data = self.payload()
                data["opening"]["unit_cost_fcfa"] = cost
                self.assertEqual(self.create(data).status_code, 400)
        self.assertEqual(self.client.get(self.url()).data["count"], 0)

    def test_largest_representable_initial_cost_is_exact(self):
        data = self.payload()
        data["opening"]["unit_cost_fcfa"] = "9223372036854.775807"
        self.assertEqual(self.create(data).status_code, 201)
        self.assertEqual(StockMovement.objects.get(product_id=data["id"]).unit_cost_fcfa, Decimal("9223372036854.775807"))
