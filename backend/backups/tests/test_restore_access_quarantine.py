"""Quarantine must replace archived approvals with fresh, scoped pending reviews."""
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from backups.models import RestoreAccessDecision, RestoreAccessGeneration, RestoreAccessReview
from backups.services.restore_quarantine import _suspend
from identity.models import Membership, Shop, User


class RestoreAccessQuarantineTests(TransactionTestCase):
    def test_all_archived_pairs_get_new_pending_reviews_while_old_decisions_are_preserved(self):
        owner = User.objects.create_user(username="quarantine-review-owner")
        seller = User.objects.create_user(username="quarantine-review-seller")
        a = Shop.objects.create(owner=owner, name="A")
        b = Shop.objects.create(owner=owner, name="B", is_active=False)
        membership = Membership.objects.create(shop=a, user=seller, is_active=False)
        # An owner's redundant membership cannot create two competing scopes.
        Membership.objects.create(shop=a, user=owner, role="manager")
        old = RestoreAccessGeneration.objects.create(operation_id=uuid.uuid4(), backup_id=uuid.uuid4(), initiated_by=owner.id)
        flags = dict.fromkeys(("can_grant_credit", "can_collect_debt", "can_read_debt",
            "can_prepare_reminder", "can_read_cost", "can_read_cash_balance"), True)
        review = RestoreAccessReview.objects.create(generation=old, shop=a, user=owner,
            archived_role="owner", was_active=True, archived_permissions=flags)
        decision = RestoreAccessDecision.objects.create(review=review, requested_by=owner, operation_id=uuid.uuid4(),
            previous_version=0, version=1, state="confirmed", reason="Ancien contrôle")
        generation_id, operation_id, backup_id, actor_id = [uuid.uuid4() for _ in range(4)]
        instant = timezone.now().replace(tzinfo=None).isoformat(sep=" ")
        with TemporaryDirectory(prefix="restore-review-") as directory:
            candidate = Path(directory)/"candidate.sqlite3"
            with closing(sqlite3.connect(candidate)) as db:
                connection.connection.backup(db)
            _suspend(candidate, instant, generation_id=generation_id, operation_id=operation_id,
                backup_id=backup_id, requested_by=actor_id)
            with closing(sqlite3.connect(candidate)) as db:
                current = db.execute("SELECT id,operation_id,backup_id,initiated_by FROM backups_restoreaccessgeneration WHERE current_slot=1").fetchone()
                self.assertEqual(current, tuple(value.hex for value in (generation_id, operation_id, backup_id, actor_id)))
                self.assertIsNone(db.execute("SELECT current_slot FROM backups_restoreaccessgeneration WHERE id=?", [old.id.hex]).fetchone()[0])
                rows = db.execute("SELECT shop_id,user_id,archived_role,membership_id,was_active,archived_permissions FROM backups_restoreaccessreview WHERE generation_id=?", [generation_id.hex]).fetchall()
                scopes = {(row[0],row[1]):row[2:] for row in rows}
                self.assertEqual(set(scopes), {(a.id.hex, owner.id.hex), (b.id.hex, owner.id.hex), (a.id.hex, seller.id.hex)})
                self.assertEqual(scopes[(a.id.hex,owner.id.hex)][:3], ("owner", None, 1))
                self.assertEqual(scopes[(a.id.hex,seller.id.hex)][:3], ("seller", membership.id.hex, 0))
                self.assertEqual(json.loads(scopes[(a.id.hex,seller.id.hex)][3]), {name:getattr(membership,name) for name in flags})
                self.assertEqual(db.execute("SELECT id FROM backups_restoreaccessdecision").fetchall(), [(decision.id.hex,)])
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(RestoreAccessGeneration.objects.get(current_slot=1).id, old.id)
        self.assertTrue(User.objects.get(id=owner.id).is_active)
