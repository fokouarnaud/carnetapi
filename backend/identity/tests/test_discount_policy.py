"""US-18: a shop's seller discount cap is an audited server decision."""
from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from common.models import Activity
from identity.models import DeviceSession, Installation, Membership, Shop, User
from catalogue.models import Product


class DiscountPolicyTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="discount-owner")
        self.manager = User.objects.create_user(username="discount-manager")
        self.seller = User.objects.create_user(username="discount-seller")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        self.other = Shop.objects.create(name="B", owner=self.owner)
        Membership.objects.create(shop=self.shop, user=self.manager, role="manager")
        Membership.objects.create(shop=self.shop, user=self.seller)
        self.client = APIClient()
        self.authenticate(self.owner)

    def authenticate(self, user):
        device = Installation.objects.create(user=user)
        session = DeviceSession.objects.create(installation=device, expires_at=timezone.now() + timedelta(days=30))
        token = AccessToken.for_user(user)
        token["session_id"] = str(session.id)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def url(self, shop=None):
        return f"/api/v1/shops/{(shop or self.shop).id}/discount-policy/"

    def payload(self, **changes):
        return {"operation_id": str(uuid.uuid4()), "base_version": 1,
                "reason": "Remise autorisée à cinq pour cent", "seller_discount_cap_basis_points": 500, **changes}

    def change(self, data=None, shop=None):
        return self.client.post(self.url(shop), data or self.payload(), format="json")

    def test_default_is_zero_and_only_manager_can_change_policy_with_audit_and_retry(self):
        self.authenticate(self.seller)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["seller_discount_cap_basis_points"], 0)
        self.assertEqual(self.change().status_code, 403)
        self.authenticate(self.manager)
        data = self.payload()
        response = self.change(data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["version"], 2)
        audit = Activity.objects.get(id=data["operation_id"])
        self.assertEqual(audit.details["before"]["seller_discount_cap_basis_points"], 0)
        self.assertEqual(audit.details["after"]["seller_discount_cap_basis_points"], 500)
        self.assertEqual(self.change(data).data, response.data)
        self.assertEqual(Activity.objects.count(), 1)
        self.assertEqual(self.change().status_code, 409)
        self.assertEqual(self.change({**data, "seller_discount_cap_basis_points": 1000}).status_code, 409)

    def test_current_policy_is_in_lease_and_applied_to_sales_without_altering_old_invoice(self):
        self.assertEqual(self.change().status_code, 200)
        product = Product.objects.create(shop=self.shop, name="Produit", unit_kind="piece", unit_label="pièce", sale_price_fcfa=10000)
        self.authenticate(self.seller)
        lease = self.client.post(f"/api/v1/shops/{self.shop.id}/verify-access/", {}, format="json")
        self.assertEqual(lease.status_code, 200)
        self.assertEqual(lease.data["seller_discount_cap_basis_points"], 500)
        self.assertEqual(lease.data["discount_policy_version"], 2)
        sale = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "global_discount_fcfa": 500,
                "lines": [{"id": str(uuid.uuid4()), "product_id": str(product.id), "product_version": 1, "quantity_milli": 1000}],
                "payments": [{"id": str(uuid.uuid4()), "method": "cash", "amount_fcfa": 9500}]}
        sale_url = f"/api/v1/shops/{self.shop.id}/sales/"
        first = self.client.post(sale_url, sale, format="json")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.data["net_total_fcfa"], 9500)
        self.authenticate(self.owner)
        self.assertEqual(self.change(self.payload(base_version=2, seller_discount_cap_basis_points=0)).status_code, 200)
        self.authenticate(self.seller)
        # Same installation is part of operation provenance: read the invoice after reconnecting.
        self.assertEqual(self.client.get(sale_url + sale["id"] + "/").data["net_total_fcfa"], 9500)
        fresh = {**sale, "id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4())}
        fresh["lines"] = [{**sale["lines"][0], "id": str(uuid.uuid4())}]
        fresh["payments"] = [{**sale["payments"][0], "id": str(uuid.uuid4())}]
        self.assertEqual(self.client.post(sale_url, fresh, format="json").status_code, 400)

    def test_invalid_policy_requests_do_not_mutate_and_cross_shop_is_hidden(self):
        for changes in [{"seller_discount_cap_basis_points": -1}, {"seller_discount_cap_basis_points": 10001},
                        {"seller_discount_cap_basis_points": True}, {"seller_discount_cap_basis_points": 5.5},
                        {"seller_discount_cap_basis_points": "500"}, {"reason": " "}, {"base_version": 1.0},
                        {"shop_id": str(self.other.id)}]:
            with self.subTest(changes=changes):
                self.assertEqual(self.change(self.payload(**changes)).status_code, 400)
        self.assertFalse(Activity.objects.exists())
        self.authenticate(self.manager)
        self.assertEqual(self.client.get(self.url(self.other)).status_code, 404)
        self.assertEqual(self.change(shop=self.other).status_code, 404)
        self.other.refresh_from_db()
        self.assertEqual(self.other.seller_discount_cap_basis_points, 0)

    def test_policy_retry_requires_current_manager_rights(self):
        self.authenticate(self.manager)
        data = self.payload()
        self.assertEqual(self.change(data).status_code, 200)
        member = Membership.objects.get(shop=self.shop, user=self.manager)
        member.role = "seller"
        member.save()
        self.assertEqual(self.change(data).status_code, 403)
        self.assertEqual(Activity.objects.count(), 1)
