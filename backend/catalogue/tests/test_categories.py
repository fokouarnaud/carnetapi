"""US-09: products can be grouped without granting writes to sellers."""
import uuid
from django.test import TestCase
from common.models import Activity
from stock.models import StockMovement
from catalogue.tests import test_catalogue


class CategoryTests(TestCase):
    setUp = test_catalogue.CatalogueTests.setUp
    authenticate = test_catalogue.CatalogueTests.authenticate
    product_payload = test_catalogue.CatalogueTests.payload

    def url(self, shop=None):
        return f"/api/v1/shops/{(shop or self.shop).id}/categories/"

    def create_category(self, shop=None):
        data = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Hygiène"}
        result = self.client.post(self.url(shop), data, format="json")
        self.assertEqual(result.status_code, 201)
        return data, result.data

    def test_category_creation_retry_and_product_assignment_preserve_one_identity(self):
        data, category = self.create_category()
        retry = self.client.post(self.url(), data, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, category)
        product_data = self.product_payload(category_id=category["id"], opening=None)
        product_url = f"/api/v1/shops/{self.shop.id}/products/"
        product = self.client.post(product_url, product_data, format="json")
        self.assertEqual(product.status_code, 201)
        self.assertEqual(product.data["category_id"], category["id"])
        self.authenticate(self.seller)
        self.assertEqual(self.client.get(self.url()).data["results"][0]["name"], "Hygiène")
        self.assertEqual(self.client.get(product_url).data["results"][0]["category_id"], category["id"])
        self.assertFalse(StockMovement.objects.exists())

    def test_category_from_another_shop_cannot_be_assigned(self):
        _, category = self.create_category(self.other)
        result = self.client.post(f"/api/v1/shops/{self.shop.id}/products/", self.product_payload(category_id=category["id"]), format="json")
        self.assertEqual(result.status_code, 404)
        self.assertFalse(StockMovement.objects.exists())

    def test_manager_can_reassign_and_clear_category_with_audit_and_product_version(self):
        _, category = self.create_category()
        product_data = self.product_payload(opening=None)
        url = f"/api/v1/shops/{self.shop.id}/products/"
        product = self.client.post(url, product_data, format="json")
        self.assertEqual(product.status_code, 201)
        update = {"operation_id": str(uuid.uuid4()), "base_version": 1, "reason": "Classement", "category_id": category["id"]}
        assigned = self.client.patch(url + product.data["id"] + "/", update, format="json")
        self.assertEqual(assigned.status_code, 200)
        self.assertEqual(assigned.data["category_id"], category["id"])
        self.assertEqual(assigned.data["version"], 2)
        audit = Activity.objects.get(id=update["operation_id"])
        self.assertIsNone(audit.details["before"]["category_id"])
        self.assertEqual(audit.details["after"]["category_id"], category["id"])
        cleared = self.client.patch(url + product.data["id"] + "/", {**update, "operation_id": str(uuid.uuid4()), "base_version": 2, "category_id": None}, format="json")
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(cleared.data["category_id"])

    def test_seller_cannot_create_and_foreign_category_list_is_hidden(self):
        self.authenticate(self.seller)
        self.assertEqual(self.client.post(self.url(), {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Autre"}, format="json").status_code, 403)
        self.assertEqual(self.client.get(self.url(self.other)).status_code, 404)

    def test_pre_category_product_receipt_can_be_replayed_without_changing_its_acknowledgement(self):
        data = self.product_payload(opening=None)
        url = f"/api/v1/shops/{self.shop.id}/products/"
        result = self.client.post(url, data, format="json")
        self.assertEqual(result.status_code, 201)
        event = Activity.objects.get(id=data["operation_id"])
        event.details["request"].pop("category_id", None)
        event.details["response"].pop("category_id", None)
        original = event.details["response"].copy()
        event.save(update_fields=["details"])
        retry = self.client.post(url, data, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data, original)

    def test_session_revoked_after_authentication_cannot_reclassify_a_product(self):
        from unittest.mock import patch
        from django.utils import timezone
        from identity.models import DeviceSession
        from catalogue.models import Product
        from catalogue.services.catalogue_services import update_product
        _, category = self.create_category()
        data = self.product_payload(opening=None)
        url = f"/api/v1/shops/{self.shop.id}/products/"
        self.assertEqual(self.client.post(url, data, format="json").status_code, 201)
        def revoked(*args, **kwargs):
            DeviceSession.objects.filter(installation__user=self.owner).update(revoked_at=timezone.now())
            return update_product(*args, **kwargs)
        with patch("catalogue.api.catalogue.update_product", side_effect=revoked):
            result = self.client.patch(url + data["id"] + "/", {"operation_id": str(uuid.uuid4()), "base_version": 1,
                                                               "reason": "Classement", "category_id": category["id"]}, format="json")
        self.assertEqual(result.status_code, 401)
        self.assertIsNone(Product.objects.get(id=data["id"]).category_id)
        self.assertEqual(Product.objects.get(id=data["id"]).version, 1)

    def test_category_rename_is_versioned_audited_and_retry_keeps_original_receipt(self):
        _, category = self.create_category()
        url = self.url() + category["id"] + "/"
        data = {"operation_id": str(uuid.uuid4()), "base_version": 1, "name": "Soins", "reason": "Libellé corrigé"}
        renamed = self.client.patch(url, data, format="json")
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.data["name"], "Soins")
        self.assertEqual(renamed.data["version"], 2)
        self.assertEqual(self.client.patch(url, data, format="json").data, renamed.data)
        event = Activity.objects.get(id=data["operation_id"])
        self.assertEqual(event.details["before"]["name"], "Hygiène")
        self.assertEqual(event.details["after"]["name"], "Soins")
        stale = {**data, "operation_id": str(uuid.uuid4()), "name": "Périmé"}
        self.assertEqual(self.client.patch(url, stale, format="json").status_code, 409)
        self.authenticate(self.seller)
        self.assertEqual(self.client.patch(url, {**data, "base_version": 2}, format="json").status_code, 403)
        self.assertEqual(self.client.get(url).data["name"], "Soins")
