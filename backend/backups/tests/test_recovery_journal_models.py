"""US-44: durable recovery captures are independent from business integration."""
from datetime import datetime,timezone as utc
import uuid

from django.db import IntegrityError,transaction,models
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from backups import models as backups
from backups.tests import test_recovery_session as fixtures
from cash.models import CashMovement
from common.models import Activity
from identity.models import Installation,User
from sales.models import Sale
from stock.models import StockMovement


class RecoveryJournalModelTests(TestCase):
    generation=fixtures.RecoverySessionTests.generation
    record=fixtures.RecoverySessionTests.record

    def setUp(self):
        fixtures.RecoverySessionTests.setUp(self)
        self.registry=self.record(self.generation())
        self.installation=Installation.objects.create(user=self.owner)
        self.instant=datetime(2026,10,20,9,tzinfo=utc.utc)

    def deposit(self,**changes):
        return backups.RecoveryJournalDeposit.objects.create(**{
            "session":self.registry,"shop":self.a,"operation_id":uuid.uuid4(),
            "submitted_by":self.owner,"submitted_installation":self.installation,
            "reason":"Copie du journal du téléphone retiré","format_version":1,
            "content_sha256":"a"*64,"receipt":{},**changes})

    def capture(self,deposit,**changes):
        return backups.RecoveryJournalOperation.objects.create(**{
            "session":self.registry,"shop":self.a,"first_deposit":deposit,
            "operation_id":uuid.uuid4(),"origin_installation_id":uuid.uuid4(),"origin_user_id":uuid.uuid4(),
            "operation_type":"sale","occurred_at":self.instant,"origin_recorded_at":self.instant,
            "payload":{"amount_fcfa":7500,"quantity_milli":1000},"dependencies":[str(uuid.uuid4())],
            "content_sha256":"b"*64,**changes})

    def test_preserves_unknown_origins_payload_dates_and_dependencies_without_any_business_effect(self):
        users=User.objects.count()
        before=[model.objects.count() for model in (Activity,CashMovement,StockMovement,Sale)]
        capture=self.capture(self.deposit())
        capture.refresh_from_db()
        self.assertEqual(capture.payload,{"amount_fcfa":7500,"quantity_milli":1000})
        self.assertEqual(len(capture.dependencies),1)
        self.assertEqual(capture.occurred_at,self.instant)
        self.assertEqual(capture.origin_recorded_at,self.instant)
        self.assertEqual(capture.shop_id,self.a.id)
        self.assertFalse(Installation.objects.filter(id=capture.origin_installation_id).exists())
        self.assertFalse(User.objects.filter(id=capture.origin_user_id).exists())
        self.assertEqual(User.objects.count(),users)
        self.assertEqual([model.objects.count() for model in (Activity,CashMovement,StockMovement,Sale)],before)

    def test_operation_uuid_cannot_be_captured_again_in_the_same_restore_even_from_another_shop(self):
        deposit=self.deposit()
        original=self.capture(deposit)
        with self.assertRaises(IntegrityError),transaction.atomic():
            self.capture(deposit,operation_id=original.operation_id,shop=self.b)
        with self.assertRaises(IntegrityError),transaction.atomic():
            self.deposit(operation_id=deposit.operation_id)
        self.registry.generation.current_slot=None
        self.registry.generation.save(update_fields=["current_slot"])
        following=self.record(self.generation())
        later=self.deposit(session=following)
        self.capture(later,session=following,operation_id=original.operation_id)
        self.assertEqual(backups.RecoveryJournalOperation.objects.count(),2)

    def test_stored_shapes_require_a_reason_version_object_payload_and_dependency_array(self):
        for changes in ({"reason":" "},{"format_version":0},{"format_version":2},
                        {"receipt":[]},{"content_sha256":"short"}):
            with self.subTest(deposit=changes),self.assertRaises(IntegrityError),transaction.atomic():
                self.deposit(**changes)
        deposit=self.deposit()
        # Bypass IntegerField's Python int() conversion to exercise stored affinity.
        with self.assertRaises(IntegrityError),transaction.atomic():
            backups.RecoveryJournalDeposit.objects.filter(id=deposit.id).update(
                format_version=models.Value(1.5,output_field=models.FloatField()))
        for changes in ({"payload":[]},{"payload":"string"},{"dependencies":{}},
                        {"operation_type":" "},{"content_sha256":"short"}):
            with self.subTest(capture=changes),self.assertRaises(IntegrityError),transaction.atomic():
                self.capture(deposit,**changes)

    def test_captures_protect_the_source_session_and_first_deposit_from_deletion(self):
        deposit=self.deposit()
        self.capture(deposit)
        for source in (self.registry,deposit):
            with self.subTest(source=type(source).__name__),self.assertRaises(ProtectedError):source.delete()
