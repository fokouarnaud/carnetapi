"""US-07 / G22: exceptional recovery needs three proofs and a 72-hour wait before the new number is attached."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from identity.models import DeviceSession, Installation, Membership, Shop, User


class AccountRecoveryCaseTests(TestCase):
    def setUp(self):
        self.account = User.objects.create_user(username="lost-owner", phone="+237690000001")
        self.member = User.objects.create_user(username="loyal-seller")
        self.stranger = User.objects.create_user(username="stranger")
        self.admin = User.objects.create_superuser(username="recovery-admin", password="x")
        self.shop = Shop.objects.create(name="A", owner=self.account)
        Membership.objects.create(shop=self.shop, user=self.member)
        installation = Installation.objects.create(user=self.account)
        self.session = DeviceSession.objects.create(installation=installation, expires_at=timezone.now() + timedelta(days=30))

    def open(self, actor=None, facts=3, confirming=None):
        from identity.services.account_recovery import open_recovery_case
        return open_recovery_case(self.account, "+237690000002", actor=actor or self.admin, identity_reference="CNI-42",
                                  facts_checked=facts, confirming_member=confirming or self.member, reason="Téléphone et codes perdus")

    def test_three_proofs_open_a_case_that_waits_seventy_two_hours(self):
        from identity.services.account_recovery import complete_recovery_case
        case = self.open()
        self.assertEqual(case.effective_after - case.created_at, timedelta(hours=72))
        with self.assertRaises(ValidationError):
            complete_recovery_case(case, actor=self.admin, now=case.created_at + timedelta(hours=71))
        complete_recovery_case(case, actor=self.admin, now=case.created_at + timedelta(hours=72))
        self.account.refresh_from_db()
        self.session.refresh_from_db()
        self.assertEqual(self.account.phone, "+237690000002")
        self.assertIsNotNone(self.session.revoked_at)

    def test_missing_proof_or_non_member_confirmation_is_refused(self):
        for changes in ({"facts": 2}, {"confirming": self.stranger}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.open(**changes)

    def test_only_the_technical_superadministrator_handles_a_case(self):
        with self.assertRaises(PermissionDenied):
            self.open(actor=self.member)
