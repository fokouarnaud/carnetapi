"""US-47 / G24: only the technical superadministrator reassigns a shop, with reason and proofs."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from common.models import Activity
from identity.models import Installation, Membership, Shop, User
from identity.services.access import accessible_shops


class OwnershipTransferTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="old-owner")
        self.buyer = User.objects.create_user(username="new-owner")
        self.admin = User.objects.create_superuser(username="tech-admin", password="x")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        self.proofs = {"identity_reference": "CNI-123", "facts_checked": 3, "confirming_member": "vendeur B"}

    def transfer(self, actor, reason="Vente du fonds de commerce"):
        from identity.services.ownership import transfer_ownership
        return transfer_ownership(self.shop, self.buyer, actor=actor, reason=reason, proofs=self.proofs)

    def test_owner_or_manager_cannot_reassign_ownership(self):
        manager = User.objects.create_user(username="manager")
        Membership.objects.create(shop=self.shop, user=manager, role=Membership.Role.MANAGER)
        for actor in (self.owner, manager):
            with self.subTest(actor=actor.username), self.assertRaises(PermissionDenied):
                self.transfer(actor)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.owner, self.owner)

    def test_superadmin_transfer_records_previous_owner_and_removes_old_access(self):
        record = self.transfer(self.admin)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.owner, self.buyer)
        self.assertEqual((record.previous_owner, record.new_owner, record.decided_by), (self.owner, self.buyer, self.admin))
        self.assertEqual(record.proofs, self.proofs)
        self.assertFalse(accessible_shops(self.owner, Installation.objects.create(user=self.owner)).filter(id=self.shop.id).exists())
        self.assertTrue(Activity.objects.filter(shop=self.shop, action="shop_ownership_transferred", actor=self.admin).exists())

    def test_reason_and_proofs_are_mandatory(self):
        with self.assertRaises(ValidationError):
            self.transfer(self.admin, reason="  ")
        self.proofs = {}
        with self.assertRaises(ValidationError):
            self.transfer(self.admin)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.owner, self.owner)
