"""Recovery access is opt-in with complete, typed operator configuration."""
import uuid
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase


class RecoveryWebSettingsTests(SimpleTestCase):
    def test_disabled_by_default_and_partial_or_invalid_configuration_is_refused(self):
        from config.recovery import recovery_web_options
        self.assertIsNone(recovery_web_options({}))
        base={"APP_RESTORE_RECOVERY_VAULT":"/private/proof.sqlite3",
            "APP_RESTORE_RECOVERY_BOOTSTRAP_OPERATION":str(uuid.uuid4()),"APP_RESTORE_RECOVERY_GATE_VERSION":"2"}
        for values in ({"APP_RESTORE_RECOVERY_VAULT":"/private/proof.sqlite3"},
                       {**base,"APP_RESTORE_RECOVERY_GATE_VERSION":"0"},
                       {**base,"APP_RESTORE_RECOVERY_BOOTSTRAP_OPERATION":"invalid"}):
            with self.subTest(values=values),self.assertRaises(ImproperlyConfigured):
                recovery_web_options(values)
        options=recovery_web_options(base)
        self.assertEqual(options["gate_version"],2)
        self.assertEqual(options["bootstrap_operation"],base["APP_RESTORE_RECOVERY_BOOTSTRAP_OPERATION"])
