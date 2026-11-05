"""Shop creation fixes ownership to the authenticated account and keeps one receipt."""
from datetime import timedelta
import uuid
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken
from common.models import Activity
from cash.models import CashMovement
from identity.models import DeviceSession, Installation, Membership, Shop, ShopDeviceAccess, User
from stock.models import StockMovement


class ShopCreationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="new-shop-owner")
        self.other = User.objects.create_user(username="other-shop-owner")
        self.client = APIClient()
        self.authenticate(self.owner)

    def authenticate(self, user):
        installation = Installation.objects.create(user=user)
        self.session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now()+timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(self.session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def payload(self, **changes):
        return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Ma boutique", **changes}

    def test_creation_assigns_only_current_owner_and_retry_preserves_initial_receipt(self):
        data = self.payload()
        response = self.client.post("/api/v1/shops/", data, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        shop = Shop.objects.get(id=data["id"])
        self.assertEqual(shop.owner_id, self.owner.id)
        self.assertEqual(shop.timezone, "Africa/Douala")
        self.assertTrue(shop.is_active)
        self.assertEqual(shop.seller_discount_cap_basis_points, 0)
        event = Activity.objects.get(id=data["operation_id"])
        self.assertEqual(event.shop_id, shop.id)
        self.assertEqual(event.actor_id, self.owner.id)
        self.assertEqual(event.action, "shop_created")
        self.assertEqual(event.target_id, shop.id)
        self.assertEqual(event.details["installation_id"], str(self.session.installation_id))
        Shop.objects.filter(id=shop.id).update(name="Nom ultérieur")
        retry = self.client.post("/api/v1/shops/", data, format="json")
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(retry.data, response.data)
        self.assertEqual(retry.data["name"], "Ma boutique")
        self.assertEqual(self.client.get(f"/api/v1/shops/{shop.id}/").data["name"], "Nom ultérieur")
        self.assertEqual(Shop.objects.count(), 1)
        self.assertEqual(Activity.objects.count(), 1)
        self.assertFalse(Membership.objects.exists())
        self.assertFalse(ShopDeviceAccess.objects.exists())
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(StockMovement.objects.exists())

    def test_creator_can_manage_its_new_shop_without_membership_or_lease_for_other_shops(self):
        data = self.payload()
        self.assertEqual(self.client.post("/api/v1/shops/", data, format="json").status_code, 201)
        lease = self.client.post(f"/api/v1/shops/{data['id']}/verify-access/", {}, format="json")
        self.assertEqual(lease.status_code, 200, lease.content)
        self.assertEqual(lease.data["role"], "manager")
        self.assertTrue(lease.data["permissions"]["can_read_cost"])
        self.authenticate(self.other)
        self.assertEqual(self.client.get(f"/api/v1/shops/{data['id']}/").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/shops/").data["count"], 0)

    def test_request_cannot_choose_owner_rights_or_installation_and_malformed_input_is_rejected(self):
        for extra in ({"owner_id": str(self.other.id)}, {"is_active": False}, {"seller_discount_cap_basis_points": 10000},
                {"installation_id": str(uuid.uuid4())}, {"name": "   "}, {"name": "x"*151}, {"id": "bad"}):
            with self.subTest(extra=extra):
                response = self.client.post("/api/v1/shops/", self.payload(**extra), format="json")
                self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Shop.objects.exists())
        self.assertFalse(Activity.objects.exists())

    def test_operation_and_entity_reuse_conflict_without_changing_any_owner(self):
        data = self.payload(reason="Ouverture confirmée")
        self.assertEqual(self.client.post("/api/v1/shops/", data, format="json").status_code, 201)
        self.assertEqual(self.client.post("/api/v1/shops/", {**data, "name": "Autre"}, format="json").status_code, 409)
        self.assertEqual(self.client.post("/api/v1/shops/", {**data, "operation_id": str(uuid.uuid4())}, format="json").status_code, 409)
        self.authenticate(self.other)
        self.assertEqual(self.client.post("/api/v1/shops/", data, format="json").status_code, 409)
        self.assertEqual(Shop.objects.get().owner_id, self.owner.id)
        self.assertEqual(Activity.objects.count(), 1)

    def test_same_name_can_create_independent_shops_without_commercial_quota(self):
        for _ in range(2):
            response = self.client.post("/api/v1/shops/", self.payload(), format="json")
            self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Shop.objects.filter(owner=self.owner).count(), 2)

    def test_current_session_is_required_for_creation_and_for_retry(self):
        data = self.payload()
        self.assertEqual(self.client.post("/api/v1/shops/", data, format="json").status_code, 201)
        self.session.revoked_at = timezone.now()
        self.session.save()
        for body in (data, self.payload()):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/v1/shops/", body, format="json").status_code, 401)
        self.assertEqual(Shop.objects.count(), 1)

    def test_unfold_cannot_create_reassign_or_delete_shops_outside_business_services(self):
        shop = Shop.objects.create(owner=self.owner, name="Historique")
        technical = User.objects.create_superuser(username="shop-tech", password=None)
        browser = Client()
        browser.force_login(technical)
        for url, body in ((reverse("admin:identity_shop_add"), {"name": "Sans journal", "owner": str(self.other.id), "timezone": "Africa/Douala", "is_active": "on"}),
                (reverse("admin:identity_shop_change", args=[shop.id]), {"name": "Transférée", "owner": str(self.other.id), "timezone": "Africa/Douala", "is_active": "on"}),
                (reverse("admin:identity_shop_delete", args=[shop.id]), {"post": "yes"})):
            with self.subTest(url=url):
                self.assertEqual(browser.post(url, body).status_code, 403)
        shop.refresh_from_db()
        self.assertEqual(shop.owner_id, self.owner.id)
        self.assertEqual(shop.name, "Historique")
        self.assertEqual(Shop.objects.count(), 1)
        self.assertEqual(browser.get(reverse("admin:identity_shop_change", args=[shop.id])).status_code, 200)

    def test_deactivated_shop_or_account_cannot_replay_creation_with_old_rights(self):
        data = self.payload()
        self.assertEqual(self.client.post("/api/v1/shops/", data, format="json").status_code, 201)
        Shop.objects.filter(id=data["id"]).update(is_active=False)
        self.assertEqual(self.client.post("/api/v1/shops/", data, format="json").status_code, 404)
        self.owner.is_active = False
        self.owner.save()
        self.assertEqual(self.client.post("/api/v1/shops/", self.payload(), format="json").status_code, 401)
        self.assertEqual(Shop.objects.count(), 1)
        self.assertEqual(Activity.objects.count(), 1)

    def test_another_activity_uuid_cannot_be_reused_to_create_a_shop(self):
        existing = Shop.objects.create(owner=self.owner, name="Existante")
        operation = Activity.objects.create(shop=existing, actor=self.owner,
            action="product_updated", target_id=uuid.uuid4(), reason="Autre événement")
        data = self.payload(operation_id=str(operation.id))
        self.assertEqual(self.client.post("/api/v1/shops/", data, format="json").status_code, 409)
        self.assertEqual(Shop.objects.count(), 1)
        self.assertEqual(Activity.objects.count(), 1)

    def test_seller_in_another_shop_can_own_its_new_shop_without_gaining_foreign_rights(self):
        existing = Shop.objects.create(owner=self.other, name="Employeur")
        member = Membership.objects.create(shop=existing, user=self.owner)
        data = self.payload()
        self.assertEqual(self.client.post("/api/v1/shops/", data, format="json").status_code, 201)
        self.assertEqual(Shop.objects.get(id=data["id"]).owner_id, self.owner.id)
        member.refresh_from_db()
        self.assertEqual(member.role, "seller")
        self.assertFalse(member.can_read_cost)
        self.assertEqual(self.client.post(f"/api/v1/shops/{existing.id}/discount-policy/", {
            "operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Ne doit pas être autorisé",
            "seller_discount_cap_basis_points": 1000}, format="json").status_code, 403)
