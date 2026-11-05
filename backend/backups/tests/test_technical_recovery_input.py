"""An unavailable terminal echo control must not expose a recovery credential."""
from getpass import GetPassWarning
import importlib.util
import io
from pathlib import Path
from unittest.mock import patch
import warnings

from django.test import SimpleTestCase


class TechnicalRecoveryInputTests(SimpleTestCase):
    def test_echo_fallback_is_refused_before_any_secret_is_read(self):
        path = Path(__file__).resolve().parents[3] / "tools" / "technical_recovery.py"
        spec = importlib.util.spec_from_file_location("technical_recovery_input_probe", path)
        console = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(console)
        would_echo = []
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        def unsafe_getpass(prompt):
            warnings.warn("Terminal echo unavailable", GetPassWarning)
            would_echo.append(True)
            return "synthetic-input"
        with patch.object(console.sys, "stdin", Terminal()), patch.object(console, "getpass", unsafe_getpass):
            with self.assertRaises(ValueError):
                console._secrets_from_input(False)
        self.assertEqual(would_echo, [])

    def test_json_stdin_mode_refuses_an_echoing_terminal_before_reading(self):
        path = Path(__file__).resolve().parents[3] / "tools" / "technical_recovery.py"
        spec = importlib.util.spec_from_file_location("technical_recovery_json_input_probe", path)
        console = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(console)
        class Terminal(io.StringIO):
            def isatty(self):
                return True
            def read(self, *args):
                raise AssertionError("must not read an echoing terminal")
        with patch.object(console.sys, "stdin", Terminal()):
            with self.assertRaises(ValueError):
                console._secrets_from_input(True)
