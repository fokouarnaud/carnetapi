"""US-08: actual Unfold pages translate labels, preserving records and rights."""
from html import unescape
from django.test import TestCase
from django.urls import reverse
from django.utils import translation
from common.models import Activity
from identity.models import Membership, Shop, User


class AdministrationLanguageTests(TestCase):
    def setUp(self):
        self.technical = User.objects.create_superuser(username="technical-language", password=None)
        self.owner = User.objects.create_user(username="proprietaire", phone="+237699123456")
        self.seller = User.objects.create_user(username="vendeur")
        self.shop = Shop.objects.create(owner=self.owner, name="Boutique Douala")
        self.member = Membership.objects.create(shop=self.shop, user=self.seller, role="manager")
        self.client.force_login(self.technical)

    def page(self, name, language, args=None, fieldset=None):
        with translation.override(language):
            response = self.client.get(reverse(name, args=args), HTTP_ACCEPT_LANGUAGE=language)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.wsgi_request.LANGUAGE_CODE, language)
            if fieldset:
                titles = [str(title) for title, _fields in response.context["adminform"].fieldsets if title]
                self.assertIn(fieldset, titles)
            return unescape(response.content.decode())

    def labels(self, html, labels):
        for label in labels:
            self.assertTrue(label in html, f"Missing rendered label: {label}")

    def test_shop_changelist_translates_each_request_and_keeps_entered_name(self):
        for language, labels in (("fr", ["Gestion des boutiques", "Boutiques", "Propriétaire", "Fuseau horaire"]),
                ("en", ["Shop management", "Shops", "Owner", "Time zone"]),
                ("fr", ["Gestion des boutiques", "Boutiques", "Propriétaire"])):
            with self.subTest(language=language):
                html = self.page("admin:identity_shop_changelist", language)
                self.labels(html, labels + ["Boutique Douala", "proprietaire"])

    def test_shop_detail_localizes_business_fields_and_units(self):
        for language, labels in (("fr", ["Plafond de remise vendeur (points de base)", "Version de la politique de remise", "Créé le (UTC)"]),
                ("en", ["Seller discount cap (basis points)", "Discount policy version", "Created at (UTC)"])):
            with self.subTest(language=language):
                self.labels(self.page("admin:identity_shop_change", language, [self.shop.id]), labels)

    def test_team_changelist_and_readonly_permissions_are_localized(self):
        for language, labels, permission in (("fr", ["Membres de l’équipe", "Boutique", "Rôle", "Responsable"], "Peut consulter les coûts"),
                ("en", ["Team members", "Shop", "Role", "Manager"], "Can view costs")):
            with self.subTest(language=language):
                self.labels(self.page("admin:identity_membership_changelist", language), labels)
                html = self.page("admin:identity_membership_change", language, [self.member.id])
                self.labels(html, [permission, "vendeur — Boutique Douala"])

    def test_profile_labels_follow_request_language_without_changing_language_preference(self):
        for language, labels in (("fr", ["Profil", "Téléphone", "Langue"]), ("en", ["Profile", "Phone", "Language"])):
            with self.subTest(language=language):
                self.labels(self.page("admin:core_user_change", language, [self.owner.id], fieldset=labels[0]), labels + ["+237699123456"])
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.language, "fr")
        self.assertEqual(self.owner.language_version, 1)

    def test_reading_translated_pages_does_not_change_data_or_grant_admin_access(self):
        before_shop = Shop.objects.values().get(id=self.shop.id)
        before_member = Membership.objects.values().get(id=self.member.id)
        self.page("admin:identity_shop_change", "en", [self.shop.id])
        self.page("admin:identity_membership_change", "fr", [self.member.id])
        self.assertEqual(Shop.objects.values().get(id=self.shop.id), before_shop)
        self.assertEqual(Membership.objects.values().get(id=self.member.id), before_member)
        self.assertFalse(Activity.objects.exists())
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("admin:identity_shop_changelist"), HTTP_ACCEPT_LANGUAGE="en").status_code, 302)
