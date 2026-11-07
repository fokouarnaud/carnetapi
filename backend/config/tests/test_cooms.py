"""Private SMS configuration is explicit, bounded, and never echoed."""
import json
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from django.core.exceptions import ImproperlyConfigured

from config.cooms import CoomsConfiguration, cooms_settings


PUBLIC_URL = "https://gw-89.cooms.app/api/v1/sms/send"
FAKE_KEY = "fixture-only-cooms-key-not-a-live-credential"


class CoomsConfigurationTests(TestCase):
    def values(self, **changes):
        return {"COOMS_API_KEY": FAKE_KEY, "COOMS_SMS_SEND_URL": PUBLIC_URL, **changes}

    def test_environment_loads_without_mutating_or_disclosing_secret_in_repr(self):
        environment = self.values()
        configuration = cooms_settings(environment)
        self.assertEqual(configuration.api_key, FAKE_KEY)
        self.assertEqual(configuration.send_url, PUBLIC_URL)
        self.assertEqual(environment, self.values())
        self.assertNotIn(FAKE_KEY, repr(configuration))

    def test_private_file_is_explicit_and_environment_overrides_its_values(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fixture-cooms.json"
            path.write_text(json.dumps(self.values()), encoding="utf-8")
            configuration = cooms_settings({"COOMS_API_KEY": "fixture-environment-key"}, secret_path=path)
            self.assertEqual(configuration.api_key, "fixture-environment-key")
            self.assertEqual(configuration.send_url, PUBLIC_URL)
            self.assertEqual(cooms_settings({"APP_OTP_COOMS_CONFIG_FILE": str(path)}).api_key, FAKE_KEY)

    def test_no_configuration_does_not_read_an_implicit_file_or_enable_sms(self):
        self.assertIsNone(cooms_settings({}))

    def test_partial_empty_or_non_string_values_are_refused(self):
        for values in ({"COOMS_API_KEY": FAKE_KEY}, {"COOMS_SMS_SEND_URL": PUBLIC_URL},
                self.values(COOMS_API_KEY=""), self.values(COOMS_API_KEY=True),
                self.values(COOMS_API_KEY=100), self.values(COOMS_SMS_SEND_URL=None)):
            with self.subTest(keys=sorted(values)):
                with self.assertRaises(ImproperlyConfigured) as caught:
                    cooms_settings(values)
                self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_endpoint_requires_https_exact_path_and_a_real_cooms_subdomain(self):
        invalid = [
            "http://gw-89.cooms.app/api/v1/sms/send", "https://cooms.app/api/v1/sms/send",
            "https://gw-89.cooms.app.evil.invalid/api/v1/sms/send", "https://fakecooms.app/api/v1/sms/send",
            "https://user@gw-89.cooms.app/api/v1/sms/send", "https://gw-89.cooms.app:8443/api/v1/sms/send",
            "https://gw-89.cooms.app/api/v1/sms/send/", "https://gw-89.cooms.app/api/v1/sms/send?key=fixture",
            "https://gw-89.cooms.app/api/v1/sms/send#fragment", "https://127.0.0.1/api/v1/sms/send",
            "https://gw-89.cooms.app./api/v1/sms/send", "https://-bad.cooms.app/api/v1/sms/send",
            " https://gw-89.cooms.app/api/v1/sms/send", "https://gw-89.cooms.app/api/v1/sms/../send",
            "https://gw-89.cooms.app\\@evil.invalid/api/v1/sms/send", "https://gw-89.cooms.app\r\n/api/v1/sms/send",
        ]
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ImproperlyConfigured):
                    CoomsConfiguration(api_key=FAKE_KEY, send_url=endpoint)
        self.assertEqual(CoomsConfiguration(api_key=FAKE_KEY,
            send_url="https://gw-90.cooms.app:443/api/v1/sms/send").send_url,
            "https://gw-90.cooms.app:443/api/v1/sms/send")

    def test_header_injection_and_unbounded_credentials_are_refused(self):
        for key in (FAKE_KEY + "\r\nx-evil: yes", " " + FAKE_KEY, FAKE_KEY + "\x00", "é", "a" * 2049):
            with self.subTest(length=len(key)):
                with self.assertRaises(ImproperlyConfigured) as caught:
                    cooms_settings(self.values(COOMS_API_KEY=key))
                self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_bad_private_files_have_generic_errors_without_path_or_file_content(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sensitive-fixture-path.json"
            contents = [FAKE_KEY, json.dumps([FAKE_KEY]), json.dumps({"OTHER": FAKE_KEY}),
                '{"COOMS_API_KEY":"first","COOMS_API_KEY":"second"}', '"' + "x" * 17000 + '"']
            for content in contents:
                path.write_text(content, encoding="utf-8")
                with self.subTest(size=len(content)):
                    with self.assertRaises(ImproperlyConfigured) as caught:
                        cooms_settings({}, secret_path=path)
                    message = "".join(traceback.format_exception(caught.exception))
                    self.assertNotIn(FAKE_KEY, message)
                    self.assertNotIn(str(path), message)
            with self.assertRaises(ImproperlyConfigured) as caught:
                cooms_settings({}, secret_path=Path(directory) / "missing-private-file.json")
            self.assertNotIn(directory, str(caught.exception))
