"""A closed recovery retains late copies without changing stock or money."""
import uuid

from django.test import TestCase

from backups import models as backups
from backups.services.recovery_closures import preview_recovery_closure, close_recovery
from backups.tests import test_recovery_purchase_receipts as receipt_fixtures
from backups.tests import test_recovery_sale_hold_api as sale_fixtures
from backups.tests import test_recovery_collection_reviews as collection_fixtures
from cash.models import CashMovement
from purchasing.models import PurchaseReceipt
from stock.models import StockReservation


def close_partial(case):
    registry = backups.RestoreSession.objects.get(generation__current_slot=1)
    missing = backups.RecoveryDevice.objects.create(session=registry, shop=case.a,
        origin_installation_id=uuid.uuid4(), origin_user_id=uuid.uuid4(), label="Appareil indisponible")
    preview = preview_recovery_closure(actor=case.technical, restore_session_id=registry.id)
    return close_recovery(actor=case.technical, data={"operation_id": str(uuid.uuid4()),
        "restore_session_id": str(registry.id), "kind": "partial", "preview_token": preview["preview_token"],
        "reason": "Appareil définitivement indisponible", "missing_device_ids": [str(missing.id)]})


class ClosedRecoveryIntegrationTests(TestCase):
    def next_generation(self, case):
        backups.RestoreAccessGeneration.objects.filter(current_slot=1).update(current_slot=None)
        case.current = case.generation()
        case.registry = case.record(case.current)
        case.review(case.a, case.owner, "owner", confirmed=True)

    def test_late_purchase_copy_stays_captured_and_invalidates_preclosure_preview(self):
        case = receipt_fixtures.RecoveryPurchaseReceiptTests()
        case.setUp()
        _, invoice = case.native_purchase()
        capture = case.capture(case.receipt_source(invoice))
        old = case.preview(capture)
        self.assertTrue(old["can_apply"])
        close_partial(case)
        current = case.preview(capture)
        self.assertFalse(current["can_apply"])
        self.assertIn("recovery_closed_review_required", {row["code"] for row in current["issues"]})
        self.assertEqual(case.apply(capture, old)[0].status_code, 409)
        self.assertFalse(PurchaseReceipt.objects.exists())
        self.assertTrue(backups.RecoveryJournalOperation.objects.filter(id=capture.id).exists())

    def test_exact_integrated_receipt_retry_survives_closure_without_a_new_effect(self):
        case = receipt_fixtures.RecoveryPurchaseReceiptTests()
        case.setUp()
        _, invoice = case.native_purchase()
        capture = case.capture(case.receipt_source(invoice))
        receipt, command = case.apply(capture)
        self.assertEqual(receipt.status_code, 201, receipt.content)
        close_partial(case)
        replay = case.client.post(case.url(f"operations/{capture.id}/integrate/"), command, format="json")
        self.assertEqual((replay.status_code, replay.data), (200, receipt.data))
        self.assertEqual(PurchaseReceipt.objects.count(), 1)

    def test_new_late_sale_cannot_create_a_reservation_via_pending_review(self):
        case = sale_fixtures.RecoverySaleHoldApiTests()
        case.setUp()
        close_partial(case)
        capture = case.capture(case.source())
        preview = case.preview(capture)
        self.assertFalse(preview["can_hold"])
        self.assertFalse(preview["can_refuse"])
        self.assertEqual(case.decide(capture, preview=preview)[0].status_code, 409)
        self.assertFalse(StockReservation.objects.exists())
        self.assertFalse(backups.RecoverySaleHold.objects.exists())

    def test_new_late_collection_cannot_enter_pending_totals_via_review(self):
        case = collection_fixtures.RecoveryCollectionReviewTests()
        case.setUp()
        close_partial(case)
        capture = case.capture(case.source())
        preview = case.preview(capture)
        self.assertFalse(preview["can_mark_pending"])
        self.assertFalse(preview["can_refuse"])
        self.assertEqual(case.decide(capture, preview=preview)[0].status_code, 409)
        self.assertFalse(CashMovement.objects.exists())
        self.assertFalse(backups.RecoveryCollectionReview.objects.exists())

    def test_closure_uuid_is_still_reserved_for_integration_in_a_new_generation(self):
        case = receipt_fixtures.RecoveryPurchaseReceiptTests()
        case.setUp()
        _, invoice = case.native_purchase()
        closure, _ = close_partial(case)
        self.next_generation(case)
        capture = case.capture(case.receipt_source(invoice))
        preview = case.preview(capture)
        self.assertTrue(preview["can_apply"], preview)
        command = {"operation_id": closure["operation_id"], "previous_version": preview["version"],
                   "preview_token": preview["preview_token"], "reason": "Contrôle d’identité de commande"}
        result = case.client.post(case.url(f"operations/{capture.id}/integrate/"), command, format="json")
        self.assertEqual(result.status_code, 409, result.content)
        self.assertFalse(PurchaseReceipt.objects.exists())

    def test_closure_uuid_is_still_reserved_for_a_pending_sale_in_a_new_generation(self):
        case = sale_fixtures.RecoverySaleHoldApiTests()
        case.setUp()
        closure, _ = close_partial(case)
        self.next_generation(case)
        capture = case.capture(case.source())
        preview = case.preview(capture)
        self.assertTrue(preview["can_hold"], preview)
        response, _ = case.decide(capture, preview=preview, operation_id=closure["operation_id"])
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(StockReservation.objects.exists())
