"""US-15: personal shop preferences suppress notifications, never the alert center."""
import uuid
from django.test import TestCase
from stock.tests import test_stock_thresholds as threshold_tests


class StockNotificationTests(TestCase):
    setUp = threshold_tests.StockThresholdApiTests.setUp
    authenticate = threshold_tests.StockThresholdApiTests.authenticate
    payload = threshold_tests.StockThresholdApiTests.payload
    create = threshold_tests.StockThresholdApiTests.create
    url = threshold_tests.StockThresholdApiTests.url

    def preference_url(self, shop=None):
        return f"/api/v1/shops/{(shop or self.shop).id}/stock-notification-preference/"

    def events_url(self, shop=None):
        return f"/api/v1/shops/{(shop or self.shop).id}/stock-alert-events/"

    def preference(self, enabled):
        return {"operation_id": str(uuid.uuid4()), "notifications_enabled": enabled}

    def test_default_enabled_then_disabling_is_personal_and_keeps_center(self):
        data, _ = self.create(stock_threshold_milli=0)
        response = self.client.get(self.preference_url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["notifications_enabled"])
        change = self.preference(False)
        response = self.client.patch(self.preference_url(), change, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["notifications_enabled"])
        self.assertEqual(self.client.patch(self.preference_url(), change, format="json").data, response.data)
        self.assertEqual(self.client.get(self.events_url()).data["count"], 0)
        center = self.client.get(f"/api/v1/shops/{self.shop.id}/stock-alerts/")
        self.assertEqual(center.data["count"], 1)
        self.authenticate(self.manager)
        self.assertTrue(self.client.get(self.preference_url()).data["notifications_enabled"])
        events = self.client.get(self.events_url())
        self.assertEqual(events.status_code, 200)
        self.assertEqual(events.data["count"], 1)
        self.assertEqual(events.data["results"][0]["product_id"], data["id"])
        self.assertTrue(events.data["results"][0]["provisional"])

    def test_current_rights_and_installation_revocation_are_checked_before_feed_or_preferences(self):
        from identity.models import ShopDeviceAccess
        from django.utils import timezone
        self.create(stock_threshold_milli=0)
        self.authenticate(self.seller)
        for endpoint in (self.preference_url(), self.events_url()):
            self.assertEqual(self.client.get(endpoint).status_code, 403)
        self.assertEqual(self.client.patch(self.preference_url(), self.preference(False), format="json").status_code, 403)
        self.assertEqual(self.client.get(self.events_url(self.other)).status_code, 404)
        session = self.authenticate(self.manager)
        ShopDeviceAccess.objects.create(shop=self.shop, installation=session.installation, revoked_at=timezone.now())
        self.assertEqual(self.client.get(self.events_url()).status_code, 404)
        self.assertEqual(self.client.get(self.preference_url()).status_code, 404)

    def test_preferences_are_strict_and_changed_retry_does_not_override_saved_choice(self):
        for invalid in ("false", 0, None):
            response = self.client.patch(self.preference_url(), self.preference(invalid), format="json")
            self.assertEqual(response.status_code, 400)
        change = self.preference(False)
        self.assertEqual(self.client.patch(self.preference_url(), change, format="json").status_code, 200)
        self.assertEqual(self.client.patch(self.preference_url(), {**change, "notifications_enabled": True}, format="json").status_code, 409)
        self.assertFalse(self.client.get(self.preference_url()).data["notifications_enabled"])

    def test_feed_is_stable_paginated_and_only_current_low_episodes_are_candidates(self):
        from catalogue.models import Product
        data, _ = self.create(stock_threshold_milli=0)
        other, _ = self.create(stock_threshold_milli=0)
        response = self.client.get(self.events_url(), {"page_size": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(len(response.data["results"]), 1)
        second = self.client.get(response.data["next"])
        self.assertNotEqual(response.data["results"][0]["id"], second.data["results"][0]["id"])
        Product.objects.filter(id=data["id"]).update(is_active=False)
        self.assertEqual(self.client.get(self.events_url()).data["count"], 1)

    def test_feed_excludes_old_episode_after_replenishment_and_new_crossing(self):
        from common.models import Activity
        from stock.models import StockAlertEvent, StockMovement
        from stock.services.stock_alert_services import observe_stock
        from django.utils import timezone
        data, _ = self.create(stock_threshold_milli=0)
        old = StockAlertEvent.objects.get().id
        for delta in (1000, -1000):
            event = Activity.objects.create(shop=self.shop, actor=self.owner, action="stock_fixture",
                                            target_id=data["id"], reason="Mouvement connu")
            StockMovement.objects.create(product_id=data["id"], operation=event, actor=self.owner,
                                         kind="purchase_receipt", quantity_milli=delta,
                                         occurred_at=timezone.now(), reason="Mouvement connu")
            observe_stock([uuid.UUID(data["id"])], event)
        feed = self.client.get(self.events_url())
        self.assertEqual(feed.data["count"], 1)
        self.assertEqual(feed.data["results"][0]["episode"], 2)
        self.assertNotEqual(feed.data["results"][0]["id"], str(old))
        self.assertEqual(self.client.get(self.events_url()).data, feed.data)
        self.assertEqual(StockAlertEvent.objects.count(), 2)

    def test_preference_does_not_change_another_shop_and_old_retry_keeps_original_receipt(self):
        disabled = self.preference(False)
        response = self.client.patch(self.preference_url(), disabled, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.client.get(self.preference_url(self.other)).data["notifications_enabled"])
        self.assertEqual(self.client.patch(self.preference_url(), self.preference(True), format="json").status_code, 200)
        self.assertEqual(self.client.patch(self.preference_url(), disabled, format="json").data, response.data)
        self.assertTrue(self.client.get(self.preference_url()).data["notifications_enabled"])
