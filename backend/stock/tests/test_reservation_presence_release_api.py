"""US-37 / G19: POST /reservations/{id}/presence-release/ — manager, refused reservation, bounded quantity."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from catalogue.models import Product
from common.models import Activity
from identity.models import DeviceSession, Installation, Membership, Shop, User
from stock.models import StockMovement, StockReservation


class PresenceReleaseApiTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="release-owner")
        self.seller = User.objects.create_user(username="release-seller")
        self.shop = Shop.objects.create(owner=self.owner, name="A")
        Membership.objects.create(shop=self.shop, user=self.seller)
        self.product = Product.objects.create(shop=self.shop, name="Savon", unit_kind="piece", unit_label="pièce", sale_price_fcfa=100)
        event = Activity.objects.create(shop=self.shop, actor=self.owner, action="sale_refused", target_id=self.product.id, reason="Refus")
        self.reservation = StockReservation.objects.create(product=self.product, operation=event, quantity_milli=3000, state="rejected")
        self.client = APIClient()

    def login(self, user):
        installation = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def release(self, quantity, **changes):
        data = {"operation_id": str(uuid.uuid4()), "quantity_milli": quantity, "reason": "Marchandise présente", **changes}
        url = f"/api/v1/shops/{self.shop.id}/reservations/{self.reservation.id}/presence-release/"
        return self.client.post(url, data, format="json"), data, url

    def test_manager_releases_up_to_the_remaining_quantity_without_movement(self):
        self.login(self.owner)
        response, data, url = self.release(2000)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["remaining_reserved_milli"], 1000)
        self.assertEqual(self.client.post(url, data, format="json").status_code, 200)
        self.assertEqual(self.release(2000)[0].status_code, 400)
        self.assertFalse(StockMovement.objects.exists())

    def test_seller_and_pending_reservation_are_refused(self):
        self.login(self.seller)
        self.assertEqual(self.release(1000)[0].status_code, 403)
        StockReservation.objects.filter(id=self.reservation.id).update(state="pending")
        self.login(self.owner)
        self.assertEqual(self.release(1000)[0].status_code, 400)
