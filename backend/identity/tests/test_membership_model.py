"""Membership invariants must also hold for imports and direct ORM writes."""
from django.db import IntegrityError, transaction
from django.test import TestCase

from identity.models import Membership, Shop, User


class MembershipModelTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="model-owner")
        self.user = User.objects.create_user(username="model-member")
        self.shop = Shop.objects.create(name="Model", owner=self.owner)

    def test_database_refuses_unsupported_role_without_granting_membership(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Membership.objects.create(shop=self.shop, user=self.user, role="owner")
        self.assertFalse(Membership.objects.exists())

    def test_database_refuses_zero_rights_version(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Membership.objects.create(shop=self.shop, user=self.user, version=0)
        self.assertFalse(Membership.objects.exists())
