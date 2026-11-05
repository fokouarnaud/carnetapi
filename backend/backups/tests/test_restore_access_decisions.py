"""Manual technical confirmation never restores global archived privileges."""
import uuid
from django.test import TestCase
from rest_framework.exceptions import PermissionDenied, ValidationError
from backups.models import RestoreAccessDecision, RestoreAccessGeneration, RestoreAccessReview
from common.services.errors import OperationConflict
from identity.models import Installation, Membership, Shop, User
from identity.services.access import accessible_shops


class RestoreAccessDecisionTests(TestCase):
    def test_current_manager_can_be_confirmed_even_when_the_archive_called_them_a_seller(self):
        user=User.objects.create_user(username="currently-manager",is_active=False)
        membership=Membership.objects.create(shop=self.a,user=user,role="manager",is_active=False)
        review=self.review(self.a,user,archived_role="seller",membership=membership)
        decision,created=self.decide(review_id=review.id)
        self.assertTrue(created)
        self.assertEqual(decision.state,"confirmed")
        membership.refresh_from_db()
        self.assertTrue(membership.is_active)
        self.assertEqual(RestoreAccessReview.objects.get(id=review.id).archived_role,"seller")

    def test_a_manager_demoted_to_seller_uses_seller_authority_for_confirmation_and_refusal(self):
        user=User.objects.create_user(username="currently-seller",is_active=False)
        membership=Membership.objects.create(shop=self.a,user=user,role="seller",is_active=False)
        review=self.review(self.a,user,archived_role="manager",membership=membership)
        for state in ("confirmed","refused"):
            with self.subTest(state=state),self.assertRaises(ValidationError):
                self.decide(review_id=review.id,operation_id=uuid.uuid4(),state=state)
        self.assertFalse(RestoreAccessDecision.objects.filter(review=review).exists())

    def setUp(self):
        self.technical = User.objects.create_superuser(username="current-tech", password=None)
        self.owner = User.objects.create_user(username="suspended-owner", is_active=False)
        self.a = Shop.objects.create(owner=self.owner, name="A")
        self.b = Shop.objects.create(owner=self.owner, name="B")
        self.installation = Installation.objects.create(user=self.owner)
        self.generation = RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(), backup_id=uuid.uuid4(), initiated_by=uuid.uuid4())
        self.flags = dict.fromkeys(("can_grant_credit", "can_collect_debt", "can_read_debt",
            "can_prepare_reminder", "can_read_cost", "can_read_cash_balance"), True)
        self.review_a = self.review(self.a, self.owner)
        self.review_b = self.review(self.b, self.owner)
        self.operation_id = uuid.uuid4()

    def review(self, shop, user, **values):
        return RestoreAccessReview.objects.create(generation=self.generation, shop=shop, user=user,
            archived_permissions=self.flags, was_active=True, **{"archived_role":"owner", **values})

    def decide(self, **values):
        from backups.services.access_decisions import decide_responsible_access
        return decide_responsible_access(**{"actor":self.technical, "review_id":self.review_a.id,
            "operation_id":self.operation_id, "previous_version":0, "state":"confirmed",
            "reason":"Identité et qualité actuelles contrôlées", "evidence_ref":"", **values})

    def test_confirmation_is_motivated_idempotent_and_only_opens_the_selected_shop(self):
        decision, created = self.decide()
        self.assertTrue(created)
        self.assertEqual(decision.requested_by_id, self.technical.id)
        self.assertEqual(decision.evidence_ref, "")
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)
        self.assertFalse(self.owner.is_staff)
        self.assertFalse(self.owner.is_superuser)
        self.assertEqual(list(accessible_shops(self.owner, self.installation)), [self.a])
        replay, created = self.decide()
        self.assertFalse(created)
        self.assertEqual(replay.id, decision.id)
        with self.assertRaises(OperationConflict):
            self.decide(reason="Un autre contrôle")
        with self.assertRaises(OperationConflict):
            self.decide(operation_id=uuid.uuid4())
        self.assertEqual(RestoreAccessDecision.objects.count(), 1)

    def test_current_technical_authority_and_reason_are_required_before_any_effect(self):
        for values in ({"actor":self.owner}, {"reason":"   "}, {"evidence_ref":"x"*501},
                       {"state":"pending"}, {"previous_version":True}):
            with self.subTest(values=values), self.assertRaises((PermissionDenied, ValidationError)):
                self.decide(**values)
        User.objects.filter(id=self.technical.id).update(is_superuser=False)
        with self.assertRaises(PermissionDenied):
            self.decide()  # Stale actor instance still has the old flag.
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)
        self.assertFalse(RestoreAccessDecision.objects.exists())

    def test_manager_confirmation_preserves_role_and_does_not_resurrect_devices_or_other_memberships(self):
        manager = User.objects.create_user(username="suspended-manager", is_active=False)
        membership = Membership.objects.create(shop=self.a, user=manager, role="manager", is_active=False)
        other = Membership.objects.create(shop=self.b, user=manager, role="manager", is_active=False)
        review = self.review(self.a, manager, archived_role="manager", membership=membership)
        device = Installation.objects.create(user=manager, revoked_at=self.generation.created_at)
        self.decide(review_id=review.id, evidence_ref="Contrôle interne 17")
        membership.refresh_from_db()
        other.refresh_from_db()
        device.refresh_from_db()
        self.assertTrue(membership.is_active)
        self.assertFalse(other.is_active)
        self.assertEqual(membership.role, "manager")
        self.assertIsNotNone(device.revoked_at)
        self.assertFalse(User.objects.get(id=manager.id).is_staff)

    def test_refusal_is_append_only_and_does_not_close_a_different_confirmed_shop(self):
        first, _ = self.decide()
        self.decide(review_id=self.review_b.id, operation_id=uuid.uuid4())
        refusal, _ = self.decide(operation_id=uuid.uuid4(), previous_version=1, state="refused", reason="Contrôle non concluant")
        self.assertEqual(refusal.version, 2)
        self.assertEqual(RestoreAccessDecision.objects.get(id=first.id).state, "confirmed")
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_active)
        self.assertEqual(list(accessible_shops(self.owner, self.installation)), [self.b])

    def test_seller_changed_role_and_historical_generation_cannot_be_confirmed_as_current_responsible(self):
        seller = User.objects.create_user(username="seller-review", is_active=False)
        membership = Membership.objects.create(shop=self.a, user=seller, role="seller", is_active=False)
        review = self.review(self.a, seller, archived_role="seller", membership=membership)
        with self.assertRaises(ValidationError):
            self.decide(review_id=review.id)
        Shop.objects.filter(id=self.a.id).update(owner=self.technical)
        with self.assertRaises(OperationConflict):
            self.decide()
        RestoreAccessGeneration.objects.filter(id=self.generation.id).update(current_slot=None)
        with self.assertRaises(OperationConflict):
            self.decide(review_id=self.review_b.id)
        self.assertFalse(RestoreAccessDecision.objects.exists())
