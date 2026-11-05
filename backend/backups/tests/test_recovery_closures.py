"""A technical partial closure preserves missing evidence and quarantines late facts."""
from copy import deepcopy
import uuid
from unittest.mock import patch

from django.db import DatabaseError, IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError, AuthenticationFailed

from backups import models as backups
from common.models import Activity
from common.services.errors import OperationConflict
from identity.models import User, Shop, Installation, DeviceSession


class RecoveryClosureTests(TestCase):
    def test_late_copy_counts_survive_equal_or_reversed_server_time_without_foreign_shop_data(self):
        from backups.services.recovery_status import recovery_status_for_shop
        self.capture()
        self.capture(shop=self.b)
        self.close()
        closure=backups.RecoveryClosure.objects.get()
        equal=self.capture()
        reversed_clock=self.capture()
        backups.RecoveryJournalOperation.objects.filter(id=equal.id).update(created_at=closure.created_at)
        backups.RecoveryJournalOperation.objects.filter(id=reversed_clock.id).update(
            created_at=closure.created_at-timezone.timedelta(minutes=1))
        current=recovery_status_for_shop(self.a)
        foreign=recovery_status_for_shop(self.b)
        self.assertEqual(current["late_operations"],2)
        self.assertTrue(current["late_review_required"])
        self.assertEqual(foreign["late_operations"],0)
        self.assertFalse(foreign["late_review_required"])

    def test_a_legacy_closure_without_scoped_capture_counts_does_not_claim_zero_late_copies(self):
        from backups.services.recovery_status import recovery_status_for_shop
        self.close()
        closure=backups.RecoveryClosure.objects.get()
        closure.snapshot.pop("captured_operation_counts",None)
        closure.save(update_fields=["snapshot","updated_at"])
        status=recovery_status_for_shop(self.a)
        self.assertIsNone(status["late_operations"])
        self.assertTrue(status["late_review_required"])

    def setUp(self):
        self.owner=User.objects.create_user(username="closure-owner")
        self.technical=User.objects.create_superuser(username="closure-technical",password=None)
        self.a,self.b=[Shop.objects.create(owner=self.owner,name=name) for name in ("A","B")]
        self.installation=Installation.objects.create(user=self.technical)
        self.live=DeviceSession.objects.create(installation=self.installation,expires_at=timezone.now()+timezone.timedelta(days=1))
        self.generation=backups.RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),initiated_by=self.technical.id)
        self.registry=backups.RestoreSession.objects.create(generation=self.generation,inventory_recorded_at=timezone.now())
        self.lost=backups.RecoveryDevice.objects.create(session=self.registry,shop=self.a,
            origin_installation_id=uuid.uuid4(),origin_user_id=uuid.uuid4(),label="Téléphone perdu")
        self.other=backups.RecoveryDevice.objects.create(session=self.registry,shop=self.b,
            origin_installation_id=uuid.uuid4(),origin_user_id=uuid.uuid4(),label="Téléphone privé B")

    def preview(self,actor=None):
        from backups.services.recovery_closures import preview_recovery_closure
        return preview_recovery_closure(actor=actor or self.technical,restore_session_id=self.registry.id,session=self.live)

    def command(self,**values):
        return {"operation_id":str(uuid.uuid4()),"restore_session_id":str(self.registry.id),"kind":"partial",
            "reason":"Téléphone définitivement perdu, opérations absentes non inventées",
            "missing_device_ids":[str(self.lost.id)],"preview_token":self.preview()["preview_token"],**values}

    def close(self,data=None,actor=None,session=None):
        from backups.services.recovery_closures import close_recovery
        return close_recovery(actor=actor or self.technical,data=data or self.command(),session=session or self.live)

    def capture(self,shop=None):
        shop=shop or self.a
        deposit=backups.RecoveryJournalDeposit.objects.create(session=self.registry,shop=shop,operation_id=uuid.uuid4(),
            submitted_by=self.owner,submitted_installation=Installation.objects.create(user=self.owner),
            reason="Copie survivante",format_version=1,content_sha256="a"*64,receipt={})
        return backups.RecoveryJournalOperation.objects.create(session=self.registry,shop=shop,first_deposit=deposit,
            operation_id=uuid.uuid4(),origin_installation_id=self.lost.origin_installation_id,origin_user_id=self.lost.origin_user_id,
            operation_type="unknown",occurred_at=timezone.now(),origin_recorded_at=timezone.now(),payload={},dependencies=[],content_sha256="b"*64)

    def test_partial_closure_keeps_missing_data_scoped_without_restoring_access_or_business_effects(self):
        from backups.services.recovery_status import recovery_status_for_shop
        before=(Activity.objects.count(),User.objects.count(),Installation.objects.count())
        data=self.command()
        receipt,created=self.close(data)
        self.assertTrue(created)
        self.assertEqual(receipt["state"],"closed_partial")
        self.assertEqual(receipt["reason"],data["reason"])
        self.assertEqual(receipt["missing_devices"][0]["id"],str(self.lost.id))
        self.assertFalse(receipt["exhaustive_recovery_guaranteed"])
        self.assertEqual((Activity.objects.count(),User.objects.count(),Installation.objects.count()),before)
        missing=recovery_status_for_shop(self.a)
        other=recovery_status_for_shop(self.b)
        self.assertEqual(missing["state"],"closed_partial")
        self.assertEqual(missing["missing_devices"],1)
        self.assertTrue(missing["missing_data"])
        self.assertEqual(missing["late_operations"],0)
        self.assertEqual(other["missing_devices"],0)
        self.assertFalse(other["missing_data"])
        self.assertNotIn("Téléphone perdu",str(missing))
        self.assertNotIn(str(self.lost.origin_installation_id),str(missing))
        self.assertEqual(backups.RecoveryClosure.objects.get().snapshot["known_devices"],2)

    def test_complete_closure_is_refused_without_a_reliable_device_balance_even_for_empty_registry(self):
        for devices in (True,False):
            if not devices:backups.RecoveryDevice.objects.all().delete()
            with self.subTest(devices=devices):
                with self.assertRaises(ValidationError) as raised:
                    self.close(self.command(kind="complete",missing_device_ids=[]))
                self.assertEqual(raised.exception.get_codes(),{"code":"recovery_complete_closure_unproven"})
        self.assertFalse(backups.RecoveryClosure.objects.exists())

    def test_partial_requires_recorded_inventory_an_explicit_current_missing_device_and_reason(self):
        for changes in ({"reason":" "},{"missing_device_ids":[]},{"missing_device_ids":[str(uuid.uuid4())]},
                        {"missing_device_ids":[str(self.lost.id),str(self.lost.id)]}):
            with self.subTest(changes=changes),self.assertRaises(ValidationError):self.close(self.command(**changes))
        self.registry.inventory_recorded_at=None;self.registry.save()
        with self.assertRaises(ValidationError):self.close()
        self.assertFalse(backups.RecoveryClosure.objects.exists())

    def test_new_capture_invalidates_preview_and_failed_write_leaves_no_closure(self):
        stale=self.command()
        self.capture()
        with self.assertRaises(OperationConflict):self.close(stale)
        current=self.command()
        with patch("backups.services.recovery_closures.RecoveryClosure.objects.create",side_effect=DatabaseError("interruption")):
            with self.assertRaises(DatabaseError):self.close(current)
        self.assertFalse(backups.RecoveryClosure.objects.exists())
        receipt,created=self.close(current)
        self.assertTrue(created)
        self.assertEqual(receipt["snapshot"]["captured_operations"]["count"],1)

    def test_exact_retry_returns_frozen_receipt_after_late_capture_but_changed_command_conflicts(self):
        data=self.command();receipt,_=self.close(data)
        self.capture()
        replay,created=self.close(deepcopy(data))
        self.assertFalse(created)
        self.assertEqual(replay,receipt)
        with self.assertRaises(OperationConflict):self.close({**data,"reason":"Autre motif"})
        with self.assertRaises(OperationConflict):self.close(self.command())
        self.assertEqual(backups.RecoveryClosure.objects.count(),1)

    def test_current_technical_role_session_and_generation_are_rechecked_even_on_retry(self):
        data=self.command();self.close(data)
        User.objects.filter(id=self.technical.id).update(is_superuser=False)
        with self.assertRaises(PermissionDenied):self.close(data)
        User.objects.filter(id=self.technical.id).update(is_superuser=True)
        DeviceSession.objects.filter(id=self.live.id).update(revoked_at=timezone.now())
        with self.assertRaises(AuthenticationFailed):self.close(data)
        DeviceSession.objects.filter(id=self.live.id).update(revoked_at=None)
        self.generation.current_slot=None;self.generation.save()
        with self.assertRaises(OperationConflict):self.close(data)

    def test_late_capture_is_preserved_separately_and_cannot_be_integrated_without_decided_reopening(self):
        from backups.services.recovery_closures import require_recovery_integration_open
        from backups.services.recovery_status import recovery_status_for_shop
        earlier=self.capture()
        require_recovery_integration_open(earlier)
        self.close()
        late=self.capture()
        self.assertEqual(backups.RecoveryJournalOperation.objects.count(),2)
        for capture in (earlier,late):
            with self.subTest(capture=capture.id),self.assertRaises(OperationConflict):require_recovery_integration_open(capture)
        state=recovery_status_for_shop(self.a)
        self.assertEqual(state["late_operations"],1)
        self.assertTrue(state["late_review_required"])
        self.assertEqual(state["missing_devices"],1)
        self.assertEqual(recovery_status_for_shop(self.b)["late_operations"],0)

    def test_database_refuses_a_second_closure_and_empty_or_inconsistent_declarations(self):
        values={"session":self.registry,"operation_id":uuid.uuid4(),"closed_by":self.technical,
            "closed_installation":self.installation,"kind":"partial","reason":"Appareil perdu",
            "request":{},"snapshot":{},"receipt":{},"missing_devices":[{"id":str(self.lost.id)}]}
        for changes in ({"kind":"complete"},{"reason":" "},{"request":[]},{"snapshot":[]},
                        {"receipt":[]},{"missing_devices":[]},{"missing_devices":{}}):
            with self.subTest(changes=changes),self.assertRaises(IntegrityError),transaction.atomic():
                backups.RecoveryClosure.objects.create(**{**values,**changes})
        self.close()
        with self.assertRaises(IntegrityError),transaction.atomic():backups.RecoveryClosure.objects.create(**values)

    def test_next_generation_keeps_previous_missing_data_warning_without_reusing_old_inventory(self):
        from backups.services.recovery_status import recovery_status_for_shop
        second=backups.RecoveryDevice.objects.create(session=self.registry,shop=self.a,
            origin_installation_id=uuid.uuid4(),origin_user_id=uuid.uuid4(),label="Autre téléphone perdu")
        self.close(self.command(missing_device_ids=[str(self.lost.id),str(second.id)]))
        self.generation.current_slot=None;self.generation.save()
        generation=backups.RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(),backup_id=uuid.uuid4(),initiated_by=self.technical.id)
        state=recovery_status_for_shop(self.a)
        self.assertEqual(state["generation_id"],str(generation.id))
        self.assertEqual(state["state"],"recovering")
        self.assertIsNone(state["known_devices"])
        self.assertIsNone(state["missing_devices"])
        self.assertEqual(state["historical_missing_declarations"],1)
        self.assertTrue(state["missing_data"])
        self.assertEqual(recovery_status_for_shop(self.b)["historical_missing_declarations"],0)
        self.assertIsNone(recovery_status_for_shop(self.b)["missing_data"])
