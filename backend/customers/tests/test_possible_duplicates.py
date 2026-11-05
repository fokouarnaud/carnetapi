"""US-20/Q75: duplicates are shop-local warnings, never automatic merges."""
import uuid
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from common.models import Activity
from customers.models import Customer
from customers.tests import test_clients as client_tests
from identity.models import DeviceSession
from identity.services.authentication import DeviceJWTAuthentication


class PossibleDuplicateTests(TestCase):
    setUp = client_tests.ClientDirectoryTests.setUp
    authenticate = client_tests.ClientDirectoryTests.authenticate
    url = client_tests.ClientDirectoryTests.url
    payload = client_tests.ClientDirectoryTests.payload
    create = client_tests.ClientDirectoryTests.create

    def duplicate_url(self, client_id, shop=None):
        return self.url(client_id, shop=shop) + "possible-duplicates/"

    def record(self, **values):
        body = self.payload(**values)
        response = self.create(body)
        self.assertEqual(response.status_code, 201, response.data)
        return body, response.data

    def test_creation_and_current_reads_warn_without_merging_or_rewriting_retry_receipts(self):
        first, first_receipt = self.record(name="Élise", phone="+237600000001")
        second, second_receipt = self.record(name="Autre nom", phone="+237600000001")
        self.assertIs(first_receipt.get("has_possible_duplicate"), False)
        self.assertIs(second_receipt.get("has_possible_duplicate"), True)
        self.assertEqual(Customer.objects.count(), 2)
        self.assertNotEqual(first["id"], second["id"])
        self.assertIs(self.client.get(self.url(first["id"])).data.get("has_possible_duplicate"), True)
        listed = self.client.get(self.url())
        self.assertEqual({row["id"] for row in listed.data["results"]}, {first["id"], second["id"]})
        self.assertTrue(all(row.get("has_possible_duplicate") is True for row in listed.data["results"]))
        self.assertEqual(self.create(first).data, first_receipt)
        # Receipts saved before this optional warning existed remain unchanged.
        event = Activity.objects.get(id=second["operation_id"])
        event.details["response"].pop("has_possible_duplicate")
        event.save(update_fields=["details"])
        self.assertEqual(self.create(second).data, event.details["response"])

    def test_missing_phone_does_not_match_and_exact_name_is_independent_of_phone(self):
        first, _ = self.record(name="Élise")
        second, _ = self.record(name="Alice", phone="")
        Customer.objects.filter(id=second["id"]).update(phone="")  # Legacy empty optional phone.
        self.assertIs(self.client.get(self.url(first["id"])).data.get("has_possible_duplicate"), False)
        self.assertIs(self.client.get(self.url(second["id"])).data.get("has_possible_duplicate"), False)
        named, receipt = self.record(name="Élise", phone="+237600000002")
        self.assertIs(receipt.get("has_possible_duplicate"), True)
        self.assertIs(self.client.get(self.url(first["id"])).data.get("has_possible_duplicate"), True)
        result = self.client.get(self.duplicate_url(first["id"]))
        self.assertEqual(result.status_code, 200, getattr(result, "data", None))
        self.assertEqual([row["id"] for row in result.data["results"]], [named["id"]])

    def test_matching_name_or_phone_counts_each_other_client_once_before_pagination(self):
        anchor, _ = self.record(name="Client commun", phone="+237600000001")
        both, _ = self.record(name="Client commun", phone="+237600000001")
        name_only, _ = self.record(name="Client commun", phone="+237600000002")
        phone_only, _ = self.record(name="Autre client", phone="+237600000001")
        self.record(name="Sans rapport", phone="+237600000003")
        Customer.objects.create(shop=self.other, name="Client commun", phone="+237600000001")
        result = self.client.get(self.duplicate_url(anchor["id"]), {"page_size": 1})
        self.assertEqual(result.status_code, 200, getattr(result, "data", None))
        self.assertEqual(result.data["count"], 3)
        ids = []
        while True:
            self.assertEqual(len(result.data["results"]), 1)
            row = result.json()["results"][0]
            ids.append(row["id"])
            self.assertEqual(row["shop_id"], str(self.shop.id))
            self.assertFalse({"debts", "balance_fcfa", "payments", "amount_fcfa"} & set(row))
            if not result.data["next"]:
                break
            result = self.client.get(result.data["next"])
        self.assertEqual(ids, [both["id"], name_only["id"], phone_only["id"]])

    def test_owner_correction_refreshes_warnings_and_keeps_original_customer_ids(self):
        first, _ = self.record(name="A", phone="+237600000001")
        second, _ = self.record(name="B", phone="+237600000002")
        self.authenticate(self.owner)
        for version, phone, warning in ((1, first["phone"], True), (2, None, False)):
            body = {"operation_id": str(uuid.uuid4()), "base_version": version, "phone": phone, "reason": "Coordonnées vérifiées"}
            result = self.client.patch(self.url(second["id"]), body, format="json")
            self.assertEqual(result.status_code, 200, getattr(result, "data", None))
            self.assertEqual(result.data["id"], second["id"])
            self.assertIs(result.data.get("has_possible_duplicate"), warning)
            self.assertIs(self.client.get(self.url(first["id"])).data.get("has_possible_duplicate"), warning)
            self.assertEqual(self.client.patch(self.url(second["id"]), body, format="json").data, result.data)
        self.assertEqual(Customer.objects.count(), 2)

    def test_lookup_is_read_only_and_cannot_cross_shop_or_keep_removed_access(self):
        first, _ = self.record(name="Même nom")
        self.record(name="Même nom")
        before = list(Customer.objects.order_by("id").values())
        activities = Activity.objects.count()
        response = self.client.get(self.duplicate_url(first["id"]))
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(list(Customer.objects.order_by("id").values()), before)
        self.assertEqual(Activity.objects.count(), activities)
        self.membership.refresh_from_db()
        self.assertFalse(self.membership.can_grant_credit)
        self.assertEqual(self.client.get(self.duplicate_url(first["id"], self.other)).status_code, 404)
        self.authenticate(self.owner)
        self.assertEqual(self.client.get(self.duplicate_url(first["id"], self.other)).status_code, 404)
        self.authenticate(self.seller)
        self.membership.is_active = False
        self.membership.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(self.duplicate_url(first["id"])).status_code, 404)

    def test_new_lookup_rechecks_a_session_revoked_after_authentication(self):
        first, _ = self.record(name="Même nom")
        self.record(name="Même nom")
        original = DeviceJWTAuthentication.authenticate

        def revoked(authenticator, request):
            result = original(authenticator, request)
            DeviceSession.objects.filter(id=request.device_session.id).update(revoked_at=timezone.now())
            return result

        with patch.object(DeviceJWTAuthentication, "authenticate", autospec=True, side_effect=revoked):
            response = self.client.get(self.duplicate_url(first["id"]))
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("results", response.data)
