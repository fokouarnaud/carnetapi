"""US-38 / G26: verifying shop access returns the server-assigned installation code, stable per shop."""
from django.test import TestCase

from identity.tests import test_device_access as device


class InstallationCodeLeaseTests(TestCase):
    setUp = device.DeviceAccessTests.setUp
    authenticate = device.DeviceAccessTests.authenticate

    def verify(self, shop):
        response = self.client.post(f"/api/v1/shops/{shop.id}/verify-access/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.data["installation_code"]

    def test_code_is_assigned_once_per_shop_and_installation(self):
        self.authenticate(self.seller)
        self.assertEqual(self.verify(self.a), "T01")
        self.assertEqual(self.verify(self.a), "T01")
        self.assertEqual(self.verify(self.b), "T01")
