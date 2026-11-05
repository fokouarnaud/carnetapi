"""Mail credentials stay in environment/private files; tests never send mail."""
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import get_connection
from django.test import SimpleTestCase


class MailConfigurationTests(SimpleTestCase):
    def load(self, values, path=None):
        return importlib.import_module("config.mail").email_settings(values, secret_path=path)

    def values(self, **changes):
        return {"EMAIL_HOST": "smtp.example.invalid", "EMAIL_PORT": "587", "EMAIL_USE_TLS": "1",
            "EMAIL_HOST_USER": "sender@example.invalid", "EMAIL_HOST_PASSWORD": "test-only-credential", **changes}

    def test_environment_values_configure_real_django_smtp_backend_and_sender(self):
        result = self.load(self.values())
        connection = get_connection(backend=result["EMAIL_BACKEND"], host=result["EMAIL_HOST"], port=result["EMAIL_PORT"],
            username=result["EMAIL_HOST_USER"], password=result["EMAIL_HOST_PASSWORD"], use_tls=result["EMAIL_USE_TLS"],
            timeout=result["EMAIL_TIMEOUT"])
        self.assertEqual(connection.host, "smtp.example.invalid")
        self.assertEqual(connection.port, 587)
        self.assertTrue(connection.use_tls)
        self.assertEqual(connection.timeout, 20)
        self.assertEqual(connection.password, "test-only-credential")
        self.assertEqual(result["DEFAULT_FROM_EMAIL"], "sender@example.invalid")
        self.assertEqual(result["SERVER_EMAIL"], "sender@example.invalid")

    def test_private_file_is_loaded_without_changing_environment_and_environment_overrides_it(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"smtp.json"
            path.write_text(json.dumps(self.values()), encoding="utf-8")
            env = {"EMAIL_HOST_PASSWORD": "new-test-only-credential"}
            result = self.load(env, path)
            self.assertEqual(result["EMAIL_HOST_PASSWORD"], "new-test-only-credential")
            self.assertEqual(result["EMAIL_HOST_USER"], "sender@example.invalid")
            self.assertEqual(env, {"EMAIL_HOST_PASSWORD": "new-test-only-credential"})

    def test_unconfigured_mail_uses_local_smtp_without_a_success_simulating_backend(self):
        result = self.load({})
        self.assertEqual(result["EMAIL_HOST"], "localhost")
        self.assertEqual(result["EMAIL_PORT"], 25)
        self.assertEqual(result["EMAIL_BACKEND"], "django.core.mail.backends.smtp.EmailBackend")
        self.assertEqual(result["EMAIL_HOST_PASSWORD"], "")

    def test_invalid_security_settings_are_rejected_without_exposing_credentials(self):
        for changes in ({"EMAIL_USE_TLS": "maybe"}, {"EMAIL_PORT": "abc"}, {"EMAIL_PORT": "0"},
                {"EMAIL_PORT": "65536"}, {"EMAIL_USE_TLS": "0"}, {"EMAIL_HOST_PASSWORD": ""}):
            with self.subTest(changes=changes):
                with self.assertRaises(ImproperlyConfigured) as caught:
                    self.load(self.values(**changes))
                self.assertNotIn("test-only-credential", str(caught.exception))

    def test_bad_or_unreadable_private_file_has_a_sanitized_error(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"smtp.json"
            for content in ("test-only-secret-malformed", '["test-only-secret"]', '{"UNSUPPORTED_SECRET":"test-only-secret"}'):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ImproperlyConfigured) as caught:
                        self.load({}, path)
                    self.assertNotIn("test-only-secret", str(caught.exception))
            with self.assertRaises(ImproperlyConfigured):
                self.load({}, Path(directory)/"missing.json")

    def test_django_settings_load_private_file_in_fresh_process(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/"smtp.json"
            path.write_text(json.dumps(self.values()), encoding="utf-8")
            env = {key:value for key,value in os.environ.items() if not key.startswith("EMAIL_")}
            env.update(APP_ENV="development", APP_EMAIL_CONFIG_FILE=str(path),
                APP_SECRET_KEY="test-only-settings-secret", APP_JWT_SIGNING_KEY="test-only-distinct-jwt-secret")
            process = subprocess.run([sys.executable, "-X", "utf8", "-c",
                "from config import settings; import json; print(json.dumps({'host':settings.EMAIL_HOST,"
                "'port':settings.EMAIL_PORT,'tls':settings.EMAIL_USE_TLS,'sender':settings.DEFAULT_FROM_EMAIL}))"],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(process.stdout), {"host":"smtp.example.invalid", "port":587,
                "tls":True, "sender":"sender@example.invalid"})

    def test_default_test_delivery_stays_in_memory(self):
        from django.core import mail
        from django.core.mail import EmailMessage
        message = EmailMessage("Local test only", "Never delivered externally", "sender@example.invalid", ["recipient@example.invalid"])
        self.assertEqual(message.send(), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["recipient@example.invalid"])
