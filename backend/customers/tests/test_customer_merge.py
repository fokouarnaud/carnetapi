"""US-20 / G18: a manager merges duplicates in one shop; late operations follow the redirection."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from common.models import Activity
from customers.models import Customer
from identity.models import Membership, Shop, User


class CustomerMergeTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="merge-owner")
        self.seller = User.objects.create_user(username="merge-seller")
        self.shop = Shop.objects.create(owner=self.owner, name="A")
        self.other_shop = Shop.objects.create(owner=self.owner, name="B")
        Membership.objects.create(shop=self.shop, user=self.seller)
        self.kept = Customer.objects.create(shop=self.shop, name="Awa")
        self.duplicate = Customer.objects.create(shop=self.shop, name="Awa T.")

    def merge(self, source, target, actor=None, reason="Même cliente"):
        from customers.services.merges import merge_customers
        return merge_customers(source, target, actor=actor or self.owner, reason=reason)

    def test_manager_merge_records_redirection_and_journal(self):
        self.merge(self.duplicate, self.kept)
        self.duplicate.refresh_from_db()
        self.assertEqual(self.duplicate.merged_into, self.kept)
        self.assertTrue(Activity.objects.filter(action="client_merged", target_id=self.duplicate.id, actor=self.owner).exists())

    def test_resolution_follows_chained_merges_and_keeps_the_original(self):
        from customers.services.merges import resolve_customer
        third = Customer.objects.create(shop=self.shop, name="Awa Traoré")
        self.merge(self.duplicate, self.kept)
        self.merge(self.kept, third)
        self.assertEqual(resolve_customer(self.duplicate), third)
        self.assertEqual(resolve_customer(third), third)

    def test_seller_cross_shop_self_and_blank_reason_are_refused(self):
        with self.assertRaises(PermissionDenied):
            self.merge(self.duplicate, self.kept, actor=self.seller)
        foreign = Customer.objects.create(shop=self.other_shop, name="Awa")
        for source, target, reason in ((self.duplicate, foreign, "x"), (self.duplicate, self.duplicate, "x"),
                                       (self.duplicate, self.kept, " ")):
            with self.subTest(target=target.name, reason=reason), self.assertRaises(ValidationError):
                self.merge(source, target, reason=reason)
        self.duplicate.refresh_from_db()
        self.assertIsNone(self.duplicate.merged_into)
