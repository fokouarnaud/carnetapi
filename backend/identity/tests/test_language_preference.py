"""US-08: own language choice is durable, versioned and independent of shop leases."""
import uuid
from datetime import timedelta
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from identity.models import ShopDeviceAccess, User
from stock.tests import test_stock_thresholds as threshold_tests


class LanguagePreferenceTests(TestCase):
    setUp = threshold_tests.StockThresholdApiTests.setUp
    authenticate = threshold_tests.StockThresholdApiTests.authenticate

    def endpoint(self):
        return "/api/v1/account/language/"

    def change(self, language="en", version=1):
        return {"operation_id": str(uuid.uuid4()), "base_version": version, "language": language}

    def test_model_language_is_supported_and_counter_cannot_be_zero(self):
        self.assertEqual(self.owner.language_version, 1)
        for changes in ({"language": "es"}, {"language_version": 0}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                User.objects.filter(id=self.owner.id).update(**changes)

    def test_change_persists_and_retry_does_not_renew_shop_lease(self):
        from identity.models import DeviceSession, LanguagePreferenceChange
        session = DeviceSession.objects.get(installation__user=self.owner)
        verified = timezone.now() - timedelta(days=2)
        lease = ShopDeviceAccess.objects.create(shop=self.shop, installation=session.installation,
            verified_at=verified, offline_expires_at=verified + timedelta(days=7))
        initial = self.client.get(self.endpoint())
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.data["language"], "fr")
        data = self.change()
        first = self.client.patch(self.endpoint(), data, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["language"], "en")
        self.assertEqual(first.data["version"], 2)
        self.assertEqual(self.client.patch(self.endpoint(), data, format="json").data, first.data)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.language, "en")
        lease.refresh_from_db()
        self.assertEqual(lease.verified_at, verified)
        self.assertEqual(LanguagePreferenceChange.objects.count(), 1)
        self.assertEqual(LanguagePreferenceChange.objects.get().before_language, "fr")

    def test_own_setting_is_available_to_seller_and_does_not_change_other_account(self):
        self.authenticate(self.seller)
        changed = self.client.patch(self.endpoint(), self.change(), format="json")
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.data["user_id"], str(self.seller.id))
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.language, "fr")
        self.assertEqual(self.client.patch(self.endpoint(), {**self.change(), "user_id": str(self.owner.id)}, format="json").status_code, 400)

    def test_conflict_and_old_receipt_do_not_overwrite_newer_choice(self):
        first = self.change()
        response = self.client.patch(self.endpoint(), first, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.patch(self.endpoint(), self.change("fr"), format="json").status_code, 409)
        self.assertEqual(self.client.patch(self.endpoint(), self.change("fr", 2), format="json").status_code, 200)
        self.assertEqual(self.client.patch(self.endpoint(), first, format="json").data, response.data)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.language, "fr")
        self.assertEqual(self.owner.language_version, 3)

    def test_reused_uuid_and_unsupported_language_leave_preference_unchanged(self):
        from identity.models import LanguagePreferenceChange
        original = self.change()
        self.assertEqual(self.client.patch(self.endpoint(), original, format="json").status_code, 200)
        reused = self.client.patch(self.endpoint(), {**original, "language": "fr"}, format="json")
        self.assertEqual(reused.status_code, 409)
        invalid = self.client.patch(self.endpoint(), self.change("es", 2), format="json")
        self.assertEqual(invalid.status_code, 400)
        self.authenticate(self.seller)
        foreign = self.client.patch(self.endpoint(), original, format="json")
        self.assertEqual(foreign.status_code, 409)
        self.assertNotIn(str(self.owner.id), str(foreign.data))
        self.owner.refresh_from_db()
        self.seller.refresh_from_db()
        self.assertEqual((self.owner.language, self.owner.language_version), ("en", 2))
        self.assertEqual((self.seller.language, self.seller.language_version), ("fr", 1))
        self.assertEqual(LanguagePreferenceChange.objects.count(), 1)

    def test_session_revoked_after_authentication_cannot_read_or_change_language(self):
        from catalogue.tests.test_catalogue_sessions import CatalogueSessionTests
        from identity.models import LanguagePreferenceChange
        for method in ("get", "patch"):
            with self.subTest(method=method):
                self.authenticate(self.owner)
                with CatalogueSessionTests.revocation(self):
                    response = (self.client.get(self.endpoint()) if method == "get" else
                                self.client.patch(self.endpoint(), self.change(), format="json"))
                self.assertEqual(response.status_code, 401)
                self.assertNotIn("language", response.data)
        self.owner.refresh_from_db()
        self.assertEqual((self.owner.language, self.owner.language_version), ("fr", 1))
        self.assertEqual(LanguagePreferenceChange.objects.count(), 0)
