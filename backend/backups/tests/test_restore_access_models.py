"""US-43: durable review scope and motivated decisions survive a new restore."""
import uuid
from django.db import IntegrityError, transaction, connection
from django.test import TestCase
from backups import models as backup_models
from identity.models import User, Shop, Membership


class RestoreAccessModelTests(TestCase):
    def test_current_tables_can_be_checked_with_native_json_constraints(self):
        from django.apps import apps
        from backups.services.restore_migrations import check_current_tables
        check_current_tables(connection, apps)

    def setUp(self):
        self.owner = User.objects.create_user(username="review-owner")
        self.technical = User.objects.create_superuser(username="review-technical", password=None)
        self.shop_a = Shop.objects.create(owner=self.owner, name="A")
        self.shop_b = Shop.objects.create(owner=self.owner, name="B")
        self.permissions = {"can_grant_credit": False, "can_collect_debt": True, "can_read_debt": False,
            "can_prepare_reminder": False, "can_read_cost": False, "can_read_cash_balance": False}

    def generation(self, **values):
        return backup_models.RestoreAccessGeneration.objects.create(**{
            "operation_id": uuid.uuid4(), "backup_id": uuid.uuid4(), "initiated_by": self.technical.id, **values})

    def review(self, generation, **values):
        return backup_models.RestoreAccessReview.objects.create(**{
            "generation": generation, "shop": self.shop_a, "user": self.owner,
            "archived_role": "owner", "was_active": True, "archived_permissions": self.permissions, **values})

    def decision(self, review, **values):
        return backup_models.RestoreAccessDecision.objects.create(**{
            "review": review, "operation_id": uuid.uuid4(), "requested_by": self.technical,
            "previous_version": 0, "version": 1, "state": "confirmed", "reason": "Contrôle actuel", **values})

    def test_one_current_generation_and_one_review_per_account_shop_preserve_old_history(self):
        current = self.generation()
        review_a = self.review(current)
        review_b = self.review(current, shop=self.shop_b)
        self.assertNotEqual(review_a.id, review_b.id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.generation()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.review(current)
        backup_models.RestoreAccessGeneration.objects.filter(id=current.id).update(current_slot=None)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.generation(operation_id=current.operation_id)
        for slot in (0, 2):
            with self.subTest(slot=slot), self.assertRaises(IntegrityError), transaction.atomic():
                self.generation(current_slot=slot)
        following = self.generation()
        self.assertNotEqual(self.review(following).id, review_a.id)
        self.assertEqual(backup_models.RestoreAccessReview.objects.filter(generation=current).count(), 2)

    def test_archived_role_membership_and_native_boolean_permission_snapshot_are_constrained(self):
        generation = self.generation()
        manager = User.objects.create_user(username="review-manager")
        membership = Membership.objects.create(shop=self.shop_a, user=manager, role="manager", is_active=False)
        self.review(generation, user=manager, membership=membership, archived_role="manager", was_active=False)
        invalid = [
            {"archived_role": "manager"}, {"archived_role": "unknown"}, {"membership": membership},
            {"archived_permissions": {**self.permissions, "can_read_cost": 0}},
            {"archived_permissions": {**self.permissions, "can_read_cost": "false"}},
            {"archived_permissions": {key: value for key, value in self.permissions.items() if key != "can_read_cost"}},
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                self.review(generation, **values)
        self.review(generation)

    def test_decisions_require_a_reason_and_consecutive_local_versions_but_optional_evidence(self):
        generation = self.generation()
        review = self.review(generation)
        for values in ({"reason": ""}, {"reason": "   "}, {"version": 2}, {"previous_version": -1},
                       {"state": "pending"}):
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                self.decision(review, **values)
        first = self.decision(review, evidence_ref="")
        self.assertEqual(first.evidence_ref, "")
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            table = connection.ops.quote_name(first._meta.db_table)
            cursor.execute(f"UPDATE {table} SET previous_version=0.5,version=1.5 WHERE id=%s", [first.id.hex])
        other_review = self.review(generation, shop=self.shop_b)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.decision(other_review, operation_id=first.operation_id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.decision(review)
        second = self.decision(review, previous_version=1, version=2, state="refused", reason="Situation actuelle corrigée")
        self.assertEqual(list(backup_models.RestoreAccessDecision.objects.filter(review=review).order_by("version")
                              .values_list("state", "reason")),
                         [("confirmed", "Contrôle actuel"), ("refused", "Situation actuelle corrigée")])
        self.assertNotEqual(first.id, second.id)
