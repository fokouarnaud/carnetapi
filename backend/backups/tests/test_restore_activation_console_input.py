"""The activation console never accepts an echoed password fallback."""
import importlib.util
from pathlib import Path
from backups.tests import test_restore_access_console_input as fixtures


class RestoreActivationInputTests(fixtures.RestoreAccessConsoleInputTests):
    def setUp(self):
        path=Path(__file__).resolve().parents[3]/"tools/activate_server_restore.py"
        spec=importlib.util.spec_from_file_location("activation_console_input",path)
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
