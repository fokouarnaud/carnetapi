"""US-06/09: cached authentication cannot outlive current session revocation on writes."""
import uuid
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from identity.services.authentication import DeviceJWTAuthentication
from catalogue.models import Barcode, Product, ProductPack
from identity.models import DeviceSession
from stock.models import StockMovement
from stock.tests import test_stock_thresholds as threshold_tests


class CatalogueSessionTests(TestCase):
    setUp = threshold_tests.StockThresholdApiTests.setUp
    authenticate = threshold_tests.StockThresholdApiTests.authenticate
    payload = threshold_tests.StockThresholdApiTests.payload
    create = threshold_tests.StockThresholdApiTests.create
    url = threshold_tests.StockThresholdApiTests.url

    def revocation(self):
        original = DeviceJWTAuthentication.authenticate
        def revoked(authenticator, request):
            result = original(authenticator, request)
            DeviceSession.objects.filter(id=request.device_session.id).update(revoked_at=timezone.now())
            return result
        return patch.object(DeviceJWTAuthentication, "authenticate", autospec=True, side_effect=revoked)

    def test_product_creation_rechecks_session_after_authentication(self):
        body = self.payload(barcode="PRODUIT", opening={"quantity_milli": 10000, "unit_cost_fcfa": None,
                            "occurred_at": "2026-01-01T10:00:00Z", "reason": "Ouverture"})
        with self.revocation():
            response = self.client.post(self.url(), body, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(Barcode.objects.count(), 0)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_pack_creation_rechecks_session_after_authentication(self):
        product, _ = self.create()
        body = {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "name": "Carton",
                "base_quantity_milli": 24000, "sale_price_fcfa": 4800, "barcode": "CARTON"}
        with self.revocation():
            response = self.client.post(self.url(product["id"]) + "packs/", body, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(ProductPack.objects.count(), 0)
        self.assertEqual(Barcode.objects.count(), 0)

    def test_product_reads_recheck_session_after_authentication(self):
        product, _ = self.create()
        for endpoint in (self.url(), self.url(product["id"])):
            self.authenticate(self.owner)
            with self.revocation():
                response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("sale_price_fcfa", response.data)

    def test_pack_list_rechecks_session_after_authentication(self):
        product, _ = self.create()
        ProductPack.objects.create(product_id=product["id"], name="Carton", base_quantity_milli=24000, sale_price_fcfa=4800)
        with self.revocation():
            response = self.client.get(self.url(product["id"]) + "packs/")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("results", response.data)

class TechnicalPermissionTests(TestCase):
    setUp = threshold_tests.StockThresholdApiTests.setUp
    authenticate = threshold_tests.StockThresholdApiTests.authenticate
    revocation = CatalogueSessionTests.revocation

    def test_documentation_checks_current_technical_role_after_authentication(self):
        from identity.models import User
        original = DeviceJWTAuthentication.authenticate
        def removed(authenticator, request):
            result = original(authenticator, request)
            User.objects.filter(id=result[0].id).update(is_superuser=False)
            return result
        for endpoint in ("/api/v1/schema/", "/api/v1/docs/", "/api/v1/redoc/"):
            User.objects.filter(id=self.owner.id).update(is_superuser=True)
            self.authenticate(self.owner)
            with patch.object(DeviceJWTAuthentication, "authenticate", autospec=True, side_effect=removed):
                response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 403)

    def test_documentation_checks_current_session_after_authentication(self):
        from identity.models import User
        User.objects.filter(id=self.owner.id).update(is_superuser=True)
        with self.revocation():
            response = self.client.get("/api/v1/schema/")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("paths", response.data)
