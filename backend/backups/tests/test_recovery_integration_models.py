"""Keep reconstruction authority separate from historical identity references."""
import uuid

from django.db import IntegrityError,transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from backups import models as backups
from backups.tests import test_recovery_journal_models as fixtures
from common.models import Activity
from identity.models import User


class RecoveryIntegrationModelTests(TestCase):
    setUp=fixtures.RecoveryJournalModelTests.setUp
    generation=fixtures.RecoveryJournalModelTests.generation
    record=fixtures.RecoveryJournalModelTests.record
    deposit=fixtures.RecoveryJournalModelTests.deposit
    capture=fixtures.RecoveryJournalModelTests.capture

    def source(self):
        capture=self.capture(self.deposit())
        operation=Activity.objects.create(id=capture.operation_id,shop=self.a,actor=self.owner,
            action="sale_created",target_id=uuid.uuid4(),reason="Reconstitution sous contrôle")
        return capture,operation

    def decision(self,capture,operation,**changes):
        return backups.RecoveryIntegrationDecision.objects.create(**{
            "capture":capture,"business_operation":operation,"operation_id":uuid.uuid4(),"reviewed_by":self.owner,
            "reviewed_installation":self.installation,"previous_version":0,"version":1,
            "outcome":"applied","reason":"Faits contrôlés","source_sha256":"a"*64,"preview_sha256":"b"*64,
            "request":{},"receipt":{},**changes})

    def test_origin_preserves_missing_identity_without_creating_or_activating_an_account(self):
        capture,operation=self.source()
        before=User.objects.count()
        origin=backups.RecoveredOperationOrigin.objects.create(capture=capture,operation=operation)
        origin.refresh_from_db()
        self.assertEqual(origin.capture.origin_user_id,capture.origin_user_id)
        self.assertEqual(origin.capture.origin_installation_id,capture.origin_installation_id)
        self.assertEqual(origin.operation.actor_id,self.owner.id)
        self.assertFalse(User.objects.filter(id=capture.origin_user_id).exists())
        self.assertEqual(User.objects.count(),before)
        with self.assertRaises(IntegrityError),transaction.atomic():
            backups.RecoveredOperationOrigin.objects.create(capture=capture,operation=operation)

    def test_approval_versions_and_command_uuid_are_unique_and_decisions_protect_history(self):
        capture,operation=self.source()
        decision=self.decision(capture,operation)
        with self.assertRaises(IntegrityError),transaction.atomic():self.decision(capture,operation)
        with self.assertRaises(IntegrityError),transaction.atomic():
            self.decision(capture,operation,operation_id=decision.operation_id,previous_version=1,version=2)
        self.decision(capture,operation,previous_version=1,version=2,outcome="already_known")
        self.assertEqual(capture.integration_decisions.count(),2)
        for source in (capture,operation):
            with self.subTest(source=type(source).__name__),self.assertRaises(ProtectedError):source.delete()

    def test_invalid_version_reason_outcome_and_receipt_shape_cannot_be_stored(self):
        capture,operation=self.source()
        for changes in ({"version":0},{"previous_version":1,"version":1},{"outcome":"validated_without_effect"},
                        {"reason":" "},{"source_sha256":"short"},{"preview_sha256":"short"},{"request":[]},{"receipt":[]}):
            with self.subTest(changes=changes),self.assertRaises(IntegrityError),transaction.atomic():
                self.decision(capture,operation,**changes)
