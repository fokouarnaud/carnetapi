"""US-15: optional stock thresholds are durable catalogue quantities."""
from django.db import IntegrityError, connection, transaction
from django.test import TestCase

from catalogue.models import Product
from identity.models import Shop, User


class StockThresholdModelTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(username="threshold-model")
        self.shop = Shop.objects.create(name="Seuil", owner=owner)

    def product(self, **changes):
        return Product.objects.create(**{ "shop": self.shop, "name": "Savon", "unit_kind": "piece",
                                         "unit_label": "pièce", "sale_price_fcfa": 250, **changes})

    def test_optional_threshold_survives_reload_and_zero_is_not_absence(self):
        product = self.product()
        product.refresh_from_db()
        self.assertIsNone(product.stock_threshold_milli)
        product.stock_threshold_milli = 0
        product.save()
        product.refresh_from_db()
        self.assertEqual(product.stock_threshold_milli, 0)
        product.stock_threshold_milli = None
        product.save()
        product.refresh_from_db()
        self.assertIsNone(product.stock_threshold_milli)

    def test_database_rejects_negative_fractional_and_partial_piece_thresholds(self):
        product = self.product()
        for value in (-1000, 1500, 1000.5):
            with self.subTest(value=value), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("UPDATE core_product SET stock_threshold_milli = %s WHERE id = %s",
                                   [value, product.id.hex])

    def test_weight_and_volume_thresholds_preserve_thousandths(self):
        for unit in ("weight", "volume"):
            product = self.product(unit_kind=unit, stock_threshold_milli=125)
            product.refresh_from_db()
            self.assertEqual(product.stock_threshold_milli, 125)

from datetime import timedelta
import uuid
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken
from common.models import Activity
from identity.models import DeviceSession, Installation, Membership
from stock.models import StockMovement


class StockThresholdApiTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="threshold-owner")
        self.manager = User.objects.create_user(username="threshold-manager")
        self.seller = User.objects.create_user(username="threshold-seller")
        self.shop = Shop.objects.create(name="Seuils", owner=self.owner)
        self.other = Shop.objects.create(name="Autre", owner=self.owner)
        Membership.objects.create(shop=self.shop, user=self.manager, role="manager")
        self.member = Membership.objects.create(shop=self.shop, user=self.seller, can_read_cost=True)
        self.client = APIClient()
        self.authenticate(self.owner)

    def authenticate(self, user):
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return session

    def url(self, product=None, shop=None):
        base = f"/api/v1/shops/{(shop or self.shop).id}/products/"
        return base if product is None else f"{base}{product}/"

    def payload(self, **changes):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Savon",
                "unit_kind": "piece", "unit_label": "pièce", "sale_price_fcfa": 250, **changes}

    def create(self, **changes):
        data = self.payload(**changes)
        response = self.client.post(self.url(), data, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return data, response

    def patch(self, product, **changes):
        return self.client.patch(self.url(product), {"operation_id": str(uuid.uuid4()),
            "base_version": 1, "reason": "Réapprovisionnement", **changes}, format="json")

    def test_create_optional_zero_and_fractional_threshold_and_seller_read(self):
        for changes, expected in [({}, None), ({"stock_threshold_milli": 0}, 0),
                                  ({"stock_threshold_milli": 8000}, 8000),
                                  ({"unit_kind": "weight", "stock_threshold_milli": 125}, 125)]:
            data, response = self.create(**changes)
            self.assertEqual(response.data["stock_threshold_milli"], expected)
            self.assertEqual(Product.objects.get(id=data["id"]).stock_threshold_milli, expected)
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.url(data["id"])).data["stock_threshold_milli"], 125)

    def test_manager_changes_and_clears_threshold_with_audit_and_retry(self):
        data, _ = self.create(stock_threshold_milli=8000)
        self.authenticate(self.manager)
        operation = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                     "reason": "Nouveau seuil", "stock_threshold_milli": 10000}
        first = self.client.patch(self.url(data["id"]), operation, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["stock_threshold_milli"], 10000)
        self.assertEqual(self.client.patch(self.url(data["id"]), operation, format="json").data, first.data)
        audit = Activity.objects.get(id=operation["operation_id"])
        self.assertEqual(audit.details["before"]["stock_threshold_milli"], 8000)
        self.assertEqual(audit.details["after"]["stock_threshold_milli"], 10000)
        self.assertEqual(self.patch(data["id"], stock_threshold_milli=9000).status_code, 409)
        operation.update(operation_id=str(uuid.uuid4()), base_version=2, stock_threshold_milli=None)
        cleared = self.client.patch(self.url(data["id"]), operation, format="json")
        self.assertEqual(cleared.status_code, 200, cleared.data)
        self.assertIsNone(cleared.data["stock_threshold_milli"])
        self.assertFalse(StockMovement.objects.exists())

    def test_seller_foreign_shop_and_withdrawn_manager_cannot_set_threshold(self):
        data, _ = self.create()
        self.authenticate(self.seller)
        self.assertEqual(self.patch(data["id"], stock_threshold_milli=8000).status_code, 403)
        self.assertEqual(self.client.post(self.url(), self.payload(stock_threshold_milli=0), format="json").status_code, 403)
        self.authenticate(self.manager)
        self.assertEqual(self.client.patch(self.url(data["id"], self.other), {}, format="json").status_code, 404)
        Membership.objects.filter(shop=self.shop, user=self.manager).update(is_active=False)
        self.assertEqual(self.patch(data["id"], stock_threshold_milli=8000).status_code, 404)
        self.assertFalse(Activity.objects.filter(action="product_updated").exists())

    def test_invalid_threshold_is_atomic_on_create_and_update(self):
        data, _ = self.create()
        for value in (-1, 1500, 1.5, True, "1000", 9223372036854775808):
            with self.subTest(value=value):
                response = self.client.post(self.url(), self.payload(stock_threshold_milli=value), format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(self.patch(data["id"], stock_threshold_milli=value).status_code, 400)
        self.assertEqual(Product.objects.count(), 1)
        self.assertFalse(Activity.objects.filter(action="product_updated").exists())

    def test_old_receipt_without_threshold_remains_replayable(self):
        data, response = self.create()
        event = Activity.objects.get(id=data["operation_id"])
        event.details["response"].pop("stock_threshold_milli")
        event.details["request"].pop("stock_threshold_milli", None)
        old = event.details["response"].copy()
        event.save(update_fields=["details"])
        replay = self.client.post(self.url(), data, format="json")
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.data, old)

    def test_weight_threshold_update_is_exact_and_changed_retry_conflicts(self):
        data, _ = self.create(unit_kind="weight", stock_threshold_milli=125)
        operation = {"operation_id": str(uuid.uuid4()), "base_version": 1,
                     "reason": "Seuil poids", "stock_threshold_milli": 126}
        result = self.client.patch(self.url(data["id"]), operation, format="json")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["stock_threshold_milli"], 126)
        operation["stock_threshold_milli"] = 127
        self.assertEqual(self.client.patch(self.url(data["id"]), operation, format="json").status_code, 409)
