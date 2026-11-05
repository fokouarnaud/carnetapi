"""A direct Unfold sign-in must return to a usable, local administration page."""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse


class AdministrationLoginRedirectTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "fixture-redirect-passphrase-53!"
        cls.user = get_user_model().objects.create_superuser(
            username="redirect-admin", password=cls.password,
        )

    def test_direct_and_explicit_returns_preserve_authentication_and_local_scope(self):
        index = reverse("admin:index")
        backups = reverse("admin:backups_backupjob_changelist")
        for next_url, expected in (
            (None, index),
            (backups, backups),
            ("https://outside.invalid/private", index),
        ):
            with self.subTest(next_url=next_url):
                client = Client(enforce_csrf_checks=True)
                login = reverse("admin:login")
                self.assertEqual(client.get(login).status_code, 200)
                data = {"username": self.user.username, "password": self.password,
                        "csrfmiddlewaretoken": client.cookies["csrftoken"].value}
                if next_url is not None:
                    data["next"] = next_url
                response = client.post(login, data)
                self.assertRedirects(response, expected, fetch_redirect_response=False)
                destination = client.get(expected)
                self.assertEqual(destination.status_code, 200)
                self.assertEqual(destination.wsgi_request.user.pk, self.user.pk)
