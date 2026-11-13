"""G26 / US-38: readable installation codes are server-assigned and never reused in a shop."""
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from identity.models import Installation, Shop, User


class InstallationCodeTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="code-owner")
        self.shop = Shop.objects.create(name="A", owner=self.owner)
        self.other = Shop.objects.create(name="B", owner=self.owner)

    def test_codes_are_consecutive_per_shop_and_idempotent_per_installation(self):
        from identity.services.installation_codes import assign_installation_code
        first, second = Installation.objects.create(user=self.owner), Installation.objects.create(user=self.owner)
        self.assertEqual(assign_installation_code(self.shop, first).label, "T01")
        self.assertEqual(assign_installation_code(self.shop, second).label, "T02")
        self.assertEqual(assign_installation_code(self.shop, first).label, "T01")
        self.assertEqual(assign_installation_code(self.other, second).label, "T01")

    def test_reinstallation_after_revocation_gets_a_new_code(self):
        from identity.services.installation_codes import assign_installation_code
        old = Installation.objects.create(user=self.owner)
        assign_installation_code(self.shop, old)
        old.revoked_at = timezone.now()
        old.save(update_fields=["revoked_at"])
        new = Installation.objects.create(user=self.owner)
        self.assertEqual(assign_installation_code(self.shop, new).label, "T02")

    def test_number_cannot_be_reused_in_the_same_shop(self):
        from identity.models import ShopInstallationCode
        first = Installation.objects.create(user=self.owner)
        ShopInstallationCode.objects.create(shop=self.shop, installation=first, number=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ShopInstallationCode.objects.create(shop=self.shop, installation=Installation.objects.create(user=self.owner), number=1)
