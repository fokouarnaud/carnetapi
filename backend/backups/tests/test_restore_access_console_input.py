"""Console password entry never accepts an echo fallback or terminal JSON."""
from getpass import GetPassWarning
import importlib.util
import io
from pathlib import Path
from unittest.mock import patch
import warnings
from django.test import SimpleTestCase


class RestoreAccessConsoleInputTests(SimpleTestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[3]/"tools/review_restore_access.py"
        spec = importlib.util.spec_from_file_location("access_console_input", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_getpass_warning_stops_before_echo_fallback(self):
        def unsafe_fallback(prompt):
            warnings.warn("Password input may be echoed.", GetPassWarning)
            self.fail("Echo fallback must never run")
        with patch.object(self.module.sys.stdin, "isatty", return_value=True), patch.object(self.module, "getpass", unsafe_fallback):
            with self.assertRaises(GetPassWarning):
                self.module._password(False)

    def test_json_on_an_interactive_terminal_is_refused_before_read(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
            def read(self, *args):
                raise AssertionError("Interactive JSON must not read echoed input")
        with patch.object(self.module.sys, "stdin", Terminal()):
            with self.assertRaises(ValueError):
                self.module._password(True)

    def test_password_input_is_bounded_and_requires_only_the_password_field(self):
        for raw in ('{}', '{"password":""}', '{"password":1}', '{"password":"x","other":"value"}',
                    '{"password":"'+'x'*4097+'"}', ' '*16385):
            with self.subTest(length=len(raw)), patch.object(self.module.sys,"stdin",io.StringIO(raw)):
                with self.assertRaises(ValueError):
                    self.module._password(True)
