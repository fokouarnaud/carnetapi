"""A recovery balance counts distinct copies without loading their private facts."""
import uuid

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from backups import models as backups
from backups.access_models import PERMISSION_FIELDS
from backups.tests import test_recovery_session as fixtures
from cash.models import CashMovement
from common.models import Activity
from identity.models import DeviceSession, Installation, User
from stock.models import StockMovement, StockReservation, StockReservationReconciliation


class BalanceFixtures:
    authenticate = fixtures.RecoverySessionTests.authenticate
    generation = fixtures.RecoverySessionTests.generation
    record = fixtures.RecoverySessionTests.record
    device = fixtures.RecoverySessionTests.device
    confirm = fixtures.RecoverySessionTests.confirm

    def setUp(self):
        fixtures.RecoverySessionTests.setUp(self)
        self.current = self.generation()
        self.registry = self.record(self.current, snapshot_at=self.snapshot_at,
                                    inventory_recorded_at=timezone.now())
        self.confirm(self.current, self.a, self.owner)
        backups.RestoreAccessReview.objects.create(generation=self.current, shop=self.b, user=self.owner,
            archived_role="owner", was_active=True, archived_permissions=dict.fromkeys(PERMISSION_FIELDS, True))
        self.client = self.authenticate(self.owner)
        self.live = DeviceSession.objects.get(installation__user=self.owner)
        self.technical_client = self.authenticate(self.technical)
        self.archived = self.device(self.registry, self.a)
        self.foreign = self.device(self.registry, self.b, label="Private B phone")

    def capture(self, *, shop=None, source=None, session=None, operation_id=None,
                operation_type="sale", digest=None):
        shop, registry = shop or self.a, session or self.registry
        origin = source or {"origin_installation_id": uuid.uuid4(), "origin_user_id": uuid.uuid4()}
        deposit = backups.RecoveryJournalDeposit.objects.create(session=registry, shop=shop,
            operation_id=uuid.uuid4(), submitted_by=self.owner, submitted_installation=self.live.installation,
            reason="Copie disponible", format_version=1, content_sha256="a" * 64,
            receipt={"private": "DO_NOT_DOWNLOAD_PRIVATE_RECEIPT"})
        return backups.RecoveryJournalOperation.objects.create(session=registry, shop=shop, first_deposit=deposit,
            operation_id=operation_id or uuid.uuid4(), operation_type=operation_type, **origin,
            occurred_at=self.snapshot_at, origin_recorded_at=timezone.now(), dependencies=[],
            payload={"private": "DO_NOT_DOWNLOAD_PRIVATE_PAYLOAD", "amount_fcfa": 987654321},
            content_sha256=digest or uuid.uuid4().hex * 2)

    def origin(self, item):
        return {"origin_installation_id": item.origin_installation_id, "origin_user_id": item.origin_user_id}

    def integration(self, capture, outcome="applied"):
        last = capture.integration_decisions.order_by("-version").first()
        version = last.version + 1 if last else 1
        event, _ = Activity.objects.get_or_create(id=capture.operation_id, defaults={
            "shop": capture.shop, "actor": self.owner, "action": "sale_created", "target_id": uuid.uuid4(),
            "reason": "Fait validé", "details": {"private": "DO_NOT_DOWNLOAD_PRIVATE_ACTIVITY"}})
        return backups.RecoveryIntegrationDecision.objects.create(capture=capture, business_operation=event,
            operation_id=uuid.uuid4(), reviewed_by=self.owner, reviewed_installation=self.live.installation,
            previous_version=version - 1, version=version, outcome=outcome, reason="Décision conservée",
            source_sha256=capture.content_sha256, preview_sha256="b" * 64,
            request={"private": "DO_NOT_DOWNLOAD_PRIVATE_REQUEST"}, receipt={"private": "DO_NOT_DOWNLOAD_PRIVATE_RECEIPT"})

    def sale_review(self, capture, states=("pending",), quantity=1000):
        event = Activity.objects.create(shop=capture.shop, actor=self.owner, action="recovery_sale_pending",
            target_id=uuid.uuid4(), reason="Examen", details={"private": "DO_NOT_DOWNLOAD_PRIVATE_ACTIVITY"})
        hold = backups.RecoverySaleHold.objects.create(capture=capture, source_operation_id=capture.operation_id,
            sale_id=uuid.uuid4(), reservation_operation=event, net_total_fcfa=500, payments_fcfa=500)
        for version, state in enumerate(states, 1):
            backups.RecoverySaleHoldDecision.objects.create(hold=hold, capture=capture, operation_id=uuid.uuid4(),
                reviewed_by=self.owner, reviewed_installation=self.live.installation, previous_version=version - 1,
                version=version, state=state, reason="Décision explicite", request={}, receipt={})
        hold.state, hold.version = states[-1], len(states)
        hold.save(update_fields=["state", "version", "updated_at"])
        reservation = StockReservation.objects.create(product=self.product, operation=event,
            quantity_milli=quantity, state="rejected" if states[-1] == "refused" else "pending")
        return hold, reservation

    def collection_review(self, capture, states=("pending",)):
        review = backups.RecoveryCollectionReview.objects.create(capture=capture,
            source_operation_id=capture.operation_id, payment_id=uuid.uuid4(), customer_id=uuid.uuid4(),
            amount_fcfa=500, original_applied_fcfa=500, original_unallocated_fcfa=0)
        for version, state in enumerate(states, 1):
            event = Activity.objects.create(shop=capture.shop, actor=self.owner, action="recovery_collection_" + state,
                target_id=review.payment_id, reason="Examen", details={"private": "DO_NOT_DOWNLOAD_PRIVATE_ACTIVITY"})
            backups.RecoveryCollectionReviewDecision.objects.create(review=review, capture=capture, operation=event,
                reviewed_by=self.owner, reviewed_installation=self.live.installation, previous_version=version - 1,
                version=version, state=state, reason="Décision explicite", request={}, receipt={})
        review.state, review.version = states[-1], len(states)
        review.save(update_fields=["state", "version", "updated_at"])
        return review

    def balance(self, shop=None):
        from backups.services.recovery_balance import balance_origins, build_balance
        origins = list(balance_origins(self.registry, shop_id=shop.id if shop else None))
        return build_balance(self.registry, origins, shop_id=shop.id if shop else None)


class RecoveryBalanceTests(BalanceFixtures, TestCase):
    def test_distinct_captures_follow_latest_recorded_decisions_not_version_count(self):
        known = self.capture(source=self.origin(self.archived))
        self.integration(known)
        self.integration(known, "already_known")
        applied = self.capture(source=self.origin(self.archived))
        self.integration(applied)
        pending = self.capture()
        self.sale_review(pending, ("pending", "pending"))
        refused = self.capture()
        self.sale_review(refused, ("pending", "refused"))
        self.capture()
        result = self.balance(self.a)
        self.assertEqual(result["summary"]["captures"], {"captured": 5, "applied": 1,
            "already_known": 1, "pending": 1, "refused": 1, "without_decision": 1})
        self.assertEqual(result["summary"]["refused_reservations_remaining"], 1)
        row = next(row for row in result["results"] if row["origin_installation_id"] == str(self.archived.origin_installation_id))
        self.assertEqual(row["captures"]["captured"], 2)
        self.assertEqual(row["captures"]["already_known"], 1)

    def test_archive_and_capture_origins_are_unioned_without_claiming_completion(self):
        self.capture(source=self.origin(self.archived))
        discovered = self.capture()
        self.capture(source=self.origin(discovered))
        self.capture(shop=self.b, source=self.origin(self.foreign))
        result = self.balance(self.a)
        self.assertEqual(len(result["results"]), 2)
        row = next(row for row in result["results"] if not row["seen_in_archived_inventory"])
        self.assertEqual(row["captures"]["captured"], 2)
        self.assertIsNone(row["journal_complete"])
        self.assertIsNone(row["coverage_since_snapshot"])
        self.assertFalse(result["summary"]["can_close_complete"])
        self.assertFalse(result["summary"]["exhaustive_recovery_guaranteed"])
        self.assertNotIn(str(self.foreign.origin_user_id), str(result))
        self.assertEqual(self.balance(self.b)["summary"]["captures"]["captured"], 1)

    def test_source_review_survives_another_generation_only_for_same_scope_and_fingerprint(self):
        old = self.capture()
        self.sale_review(old, ("pending", "refused"))
        collection = self.capture(operation_type="customer_collection")
        self.collection_review(collection, ("pending",))
        self.current.current_slot = None
        self.current.save()
        self.current = self.generation()
        self.registry = self.record(self.current)
        self.capture(source=self.origin(old), operation_id=old.operation_id, digest=old.content_sha256)
        self.capture(source=self.origin(collection), operation_id=collection.operation_id,
                     operation_type="customer_collection", digest=collection.content_sha256)
        result = self.balance(self.a)
        self.assertEqual((result["summary"]["captures"]["refused"], result["summary"]["captures"]["pending"]), (1, 1))
        self.assertEqual(result["summary"]["refused_reservations_remaining"], 1)
        backups.RecoveryJournalOperation.objects.filter(session=self.registry, operation_id=old.operation_id).update(content_sha256="f" * 64)
        changed = self.balance(self.a)
        self.assertEqual((changed["summary"]["captures"]["refused"], changed["summary"]["captures"]["without_decision"]), (0, 1))

    def test_refused_reservations_remain_visible_until_the_whole_quantity_is_reconciled(self):
        capture = self.capture()
        _hold, reservation = self.sale_review(capture, ("pending", "refused"), quantity=2000)
        event = Activity.objects.create(shop=self.a, actor=self.owner, action="stock_loss_recorded",
            target_id=uuid.uuid4(), reason="Perte constatée")
        movement = StockMovement.objects.create(product=self.product, actor=self.owner, operation=event,
            kind="loss", quantity_milli=-2000, occurred_at=timezone.now(), reason="Perte constatée")
        for quantity, expected in ((1000, 1), (1000, 0)):
            operation = Activity.objects.create(shop=self.a, actor=self.owner, action="recovery_reservation_reconciled",
                target_id=reservation.id, reason="Rapprochement")
            StockReservationReconciliation.objects.create(reservation=reservation, movement=movement,
                operation=operation, installation=self.live.installation, quantity_milli=quantity)
            self.assertEqual(self.balance(self.a)["summary"]["refused_reservations_remaining"], expected)

    def test_empty_inventory_and_native_uuid_presence_never_establish_completion_or_known_receipt(self):
        backups.RecoveryDevice.objects.all().delete()
        self.registry.inventory_recorded_at = None
        self.registry.save()
        empty = self.balance(self.a)
        self.assertEqual(empty["results"], [])
        self.assertEqual(empty["summary"]["captures"]["captured"], 0)
        self.assertIsNone(empty["summary"]["archived_device_observations"])
        self.assertFalse(empty["summary"]["can_close_complete"])
        capture = self.capture()
        Activity.objects.create(id=capture.operation_id, shop=self.a, actor=self.owner, action="sale_created",
            target_id=uuid.uuid4(), reason="UUID seul")
        result = self.balance(self.a)
        self.assertEqual(result["summary"]["captures"]["without_decision"], 1)
        self.assertEqual(result["summary"]["captures"]["already_known"], 0)
        self.assertIsNone(result["summary"]["inventory_recorded_at"])
        self.assertFalse(result["summary"]["can_close_complete"])

    def test_queries_never_download_source_payloads_money_costs_or_decision_receipts(self):
        capture = self.capture()
        self.integration(capture)
        before = (Activity.objects.count(), User.objects.count(), Installation.objects.count(),
                  StockMovement.objects.count(), CashMovement.objects.count(), backups.RecoveryClosure.objects.count())
        with CaptureQueriesContext(connection) as queries:
            result = self.balance(self.a)
        sql = "\n".join(row["sql"] for row in queries)
        for column in ("payload", "request", "receipt", "details", "net_total_fcfa", "payments_fcfa", "amount_fcfa", "unit_cost_micros"):
            self.assertNotIn('."' + column + '"', sql)
        self.assertNotIn("DO_NOT_DOWNLOAD", str(result))
        self.assertEqual(before, (Activity.objects.count(), User.objects.count(), Installation.objects.count(),
                  StockMovement.objects.count(), CashMovement.objects.count(), backups.RecoveryClosure.objects.count()))
