"""Client commands recheck the exact session inside their write transaction."""
import uuid
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from common.models import Activity
from customers.api.client_serializers import CreateClientSerializer, ReportCorrectionSerializer, UpdateClientSerializer
from customers.models import Customer
from customers.tests import test_clients as client_tests
from identity.models import DeviceSession


class ClientSessionTests(TestCase):
    setUp = client_tests.ClientDirectoryTests.setUp
    authenticate = client_tests.ClientDirectoryTests.authenticate
    url = client_tests.ClientDirectoryTests.url
    payload = client_tests.ClientDirectoryTests.payload
    create = client_tests.ClientDirectoryTests.create

    def revoked_after_validation(self, serializer_class, device):
        original = serializer_class.run_validation

        def validate(serializer, data):
            result = original(serializer, data)
            DeviceSession.objects.filter(installation=device).update(revoked_at=timezone.now())
            return result

        return patch.object(serializer_class, "run_validation", autospec=True, side_effect=validate)

    def test_creation_cannot_commit_after_session_is_revoked_during_validation(self):
        device = self.authenticate(self.seller)
        with self.revoked_after_validation(CreateClientSerializer, device):
            response = self.create(self.payload())
        self.assertEqual(response.status_code, 401)
        self.assertFalse(Customer.objects.exists())
        self.assertFalse(Activity.objects.exists())

    def test_existing_creation_receipt_is_not_released_to_a_session_revoked_during_validation(self):
        body = self.payload()
        self.assertEqual(self.create(body).status_code, 201)
        device = self.authenticate(self.seller)
        with self.revoked_after_validation(CreateClientSerializer, device):
            response = self.create(body)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("phone", response.data)
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(Activity.objects.count(), 1)

    def test_correction_cannot_commit_after_session_is_revoked_during_validation(self):
        body = self.payload()
        self.assertEqual(self.create(body).status_code, 201)
        device = self.authenticate(self.owner)
        before = Customer.objects.values().get(id=body["id"])
        update = {"operation_id": str(uuid.uuid4()), "base_version": 1, "name": "Correction", "reason": "Erreur"}
        with self.revoked_after_validation(UpdateClientSerializer, device):
            response = self.client.patch(self.url(body["id"]), update, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Customer.objects.values().get(id=body["id"]), before)
        self.assertEqual(Activity.objects.count(), 1)

    def test_report_cannot_commit_after_session_is_revoked_during_validation(self):
        body = self.payload()
        self.assertEqual(self.create(body).status_code, 201)
        device = self.authenticate(self.seller)
        report = {"operation_id": str(uuid.uuid4()), "reason": "Numéro à vérifier"}
        with self.revoked_after_validation(ReportCorrectionSerializer, device):
            response = self.client.post(self.url(body["id"]) + "report-correction/", report, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Activity.objects.count(), 1)
