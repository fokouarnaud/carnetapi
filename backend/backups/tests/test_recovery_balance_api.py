"""Recovery balance access is scoped before SQL pagination and never writes facts."""
import json
import uuid

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from drf_spectacular.generators import SchemaGenerator
from jsonschema import Draft4Validator, FormatChecker
from rest_framework.test import APIClient

from backups import models as backups
from backups.tests.test_recovery_balance import BalanceFixtures
from backups.tests.test_recovery_purchase_contract import nullable_schema
from backups.tests import test_recovery_sale_hold_api as sale_fixtures
from identity.models import DeviceSession, Membership, ShopDeviceAccess, User


class RecoveryBalanceApiTests(BalanceFixtures, TestCase):
    def url(self, shop=None):
        return f"/api/v1/shops/{(shop or self.a).id}/recovery/balance/"

    def technical_url(self, registry=None):
        return f"/api/v1/recovery/sessions/{(registry or self.registry).id}/balance/"

    def test_responsible_sees_only_own_origins_with_totals_over_all_pages(self):
        for _ in range(3):
            self.capture()
        self.capture(shop=self.b, source=self.origin(self.foreign))
        first = self.client.get(self.url(), {"page_size": 2})
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual((first.data["count"], len(first.data["results"])), (4, 2))
        self.assertEqual(first.data["summary"]["captures"]["captured"], 3)
        second = self.client.get(first.data["next"])
        self.assertEqual(second.status_code, 200, second.content)
        keys = [(row["shop_id"], row["origin_installation_id"], row["origin_user_id"])
                for row in first.data["results"] + second.data["results"]]
        self.assertEqual(len(set(keys)), 4)
        self.assertEqual(keys, sorted(keys))
        self.assertNotIn(str(self.foreign.origin_installation_id), json.dumps(first.data))
        self.assertEqual(self.client.get(self.url(self.b)).status_code, 404)

    def test_current_technical_role_can_read_global_metadata_without_business_membership(self):
        self.capture()
        self.capture(shop=self.b, source=self.origin(self.foreign))
        response = self.technical_client.get(self.technical_url())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.data["count"], response.data["summary"]["captures"]["captured"]), (3, 2))
        self.assertEqual({row["shop_id"] for row in response.data["results"]}, {str(self.a.id), str(self.b.id)})
        self.assertNotIn("DO_NOT_DOWNLOAD", json.dumps(response.data))
        self.assertEqual(self.client.get(self.technical_url()).status_code, 403)
        self.assertEqual(self.technical_client.get(self.url()).status_code, 404)
        self.technical.is_superuser = False
        self.technical.save(update_fields=["is_superuser"])
        self.assertEqual(self.technical_client.get(self.technical_url()).status_code, 403)

    def test_current_scope_session_and_restore_generation_are_required_on_every_read(self):
        self.assertEqual(APIClient().get(self.url()).status_code, 401)
        self.assertEqual(APIClient().get(self.technical_url()).status_code, 401)
        seller = User.objects.create_user(username="balance-seller")
        member = Membership.objects.create(user=seller, shop=self.a)
        self.confirm(self.current, self.a, seller, membership=member)
        self.assertEqual(self.authenticate(seller).get(self.url()).status_code, 403)
        ShopDeviceAccess.objects.create(shop=self.a, installation=self.live.installation, revoked_at=timezone.now())
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        DeviceSession.objects.filter(installation__user=self.technical).update(revoked_at=timezone.now())
        self.assertEqual(self.technical_client.get(self.technical_url()).status_code, 401)

    def test_technical_session_auth_works_and_historical_or_unknown_sessions_are_hidden(self):
        browser = APIClient(enforce_csrf_checks=True)
        browser.force_login(self.technical)
        self.assertEqual(browser.get(self.technical_url()).status_code, 200)
        browser.cookies["csrftoken"] = "a" * 32
        self.assertEqual(browser.post(self.technical_url(), {}, format="json").status_code, 403)
        for method in ("post", "patch", "delete"):
            self.assertEqual(getattr(browser, method)(self.technical_url(), {}, format="json",
                HTTP_X_CSRFTOKEN="a" * 32).status_code, 405)
        self.current.current_slot = None
        self.current.save()
        self.assertEqual(browser.get(self.technical_url()).status_code, 404)
        self.assertEqual(browser.get(f"/api/v1/recovery/sessions/{uuid.uuid4()}/balance/").status_code, 404)

    def test_identity_conflicts_are_visible_but_never_merge_different_origin_users(self):
        self.capture(source={"origin_installation_id": self.archived.origin_installation_id,
                             "origin_user_id": uuid.uuid4()})
        result = self.client.get(self.url())
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.data["count"], 2)
        self.assertTrue(all(row["origin_identity_conflict"] for row in result.data["results"]))
        self.assertEqual(sum(row["captures"]["captured"] for row in result.data["results"]), 1)

    def test_sql_pagination_is_bounded_and_does_not_download_private_payloads(self):
        for _ in range(102):
            self.capture()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url(), {"page_size": 10000})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.data["count"], len(response.data["results"])), (103, 100))
        sql = "\n".join(row["sql"] for row in queries)
        self.assertIn("LIMIT 100", sql)
        for column in ("payload", "request", "receipt", "details", "net_total_fcfa", "payments_fcfa", "amount_fcfa", "unit_cost_micros"):
            self.assertNotIn('."' + column + '"', sql)
        self.assertFalse(response.data["summary"]["can_close_complete"])
        self.assertIsNone(response.data["summary"]["inventory_exhaustive"])
        self.assertEqual(backups.RecoveryClosure.objects.count(), 0)

    def test_openapi_validates_both_actual_scopes_and_declares_a_bounded_metadata_page(self):
        self.capture()
        responses = [(self.client.get(self.url()), "/shops/{shop_id}/recovery/balance/"),
                     (self.technical_client.get(self.technical_url()), "/recovery/sessions/{session_id}/balance/")]
        for response, _suffix in responses:
            self.assertEqual(response.status_code, 200, response.content)
        schema = SchemaGenerator().get_schema(request=None, public=True)
        for response, suffix in responses:
            with self.subTest(scope=suffix):
                path = next(path for path in schema["paths"] if path.endswith(suffix))
                rule = schema["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
                validator = Draft4Validator(nullable_schema({**rule, "components": schema["components"]}), format_checker=FormatChecker())
                self.assertEqual([(list(error.absolute_path), error.message)
                                  for error in validator.iter_errors(json.loads(response.content))], [])
        page = schema["components"]["schemas"]["RecoveryBalancePage"]
        self.assertEqual(page["properties"]["results"]["maxItems"], 100)


class RecoveryBalanceRecordedWorkflowTests(TestCase):
    setUp = sale_fixtures.RecoverySaleHoldApiTests.setUp
    authenticate = sale_fixtures.RecoverySaleHoldApiTests.authenticate
    generation = sale_fixtures.RecoverySaleHoldApiTests.generation
    record = sale_fixtures.RecoverySaleHoldApiTests.record
    device = sale_fixtures.RecoverySaleHoldApiTests.device
    review = sale_fixtures.RecoverySaleHoldApiTests.review
    payload = sale_fixtures.RecoverySaleHoldApiTests.payload
    url = sale_fixtures.RecoverySaleHoldApiTests.url
    source = sale_fixtures.RecoverySaleHoldApiTests.source
    capture = sale_fixtures.RecoverySaleHoldApiTests.capture
    preview = sale_fixtures.RecoverySaleHoldApiTests.preview
    apply = sale_fixtures.RecoverySaleHoldApiTests.apply
    decide = sale_fixtures.RecoverySaleHoldApiTests.decide
    stage = sale_fixtures.RecoverySaleHoldApiTests.stage

    def test_real_pending_validation_and_second_review_count_one_capture_without_replaying_sale(self):
        from cash.models import CashMovement
        from sales.models import Sale
        from stock.models import StockMovement
        capture = self.capture(self.source())
        self.stage(capture)
        url = f"/api/v1/shops/{self.a.id}/recovery/balance/"
        pending = self.client.get(url)
        self.assertEqual(pending.status_code, 200, pending.content)
        self.assertEqual(pending.data["summary"]["captures"]["pending"], 1)
        applied, _command = self.apply(capture)
        self.assertEqual(applied.status_code, 201, applied.content)
        first = self.client.get(url)
        self.assertEqual((first.data["summary"]["captures"]["captured"], first.data["summary"]["captures"]["applied"]), (1, 1))
        known, _command = self.apply(capture)
        self.assertEqual((known.status_code, known.data["outcome"]), (201, "already_known"))
        before = (Sale.objects.count(), CashMovement.objects.count(), StockMovement.objects.count())
        final = self.client.get(url)
        self.assertEqual(final.data["summary"]["captures"], {"captured": 1, "applied": 0,
            "already_known": 1, "pending": 0, "refused": 0, "without_decision": 0})
        self.assertEqual(before, (Sale.objects.count(), CashMovement.objects.count(), StockMovement.objects.count()))
