"""Emergency console commands work without importing Django settings."""
import json
from pathlib import Path
import os
import subprocess
import sys
import uuid
from contextlib import closing
import sqlite3
from tempfile import TemporaryDirectory
from django.test import SimpleTestCase


class RestoreGateConsoleTests(SimpleTestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="restore-console-gate-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / "business.sqlite3"
        self.actor = uuid.uuid4()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE business (amount INTEGER)")

    def test_console_initialize_inspect_enter_and_abort_without_django(self):
        gate = self.root / "private" / "gate.sqlite3"
        script = Path(__file__).resolve().parents[3] / "tools" / "restore_maintenance.py"
        env = {**os.environ, "DJANGO_SETTINGS_MODULE": "intentionally.invalid.settings"}
        def run(action, *arguments):
            result = subprocess.run([sys.executable, "-I", "-X", "utf8", str(script), action,
                "--gate", str(gate), *arguments], capture_output=True, text=True,
                encoding="utf-8", env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        identity = ["--requested-by", str(self.actor), "--reason", "Contrôle console"]
        initialized = run("initialize", "--database", str(self.database), "--operation-id", str(uuid.uuid4()), *identity)
        self.assertEqual((initialized["mode"], initialized["version"]), ("open", 1))
        self.assertEqual(run("status")["mode"], "open")
        entered = run("enter", "--expected-version", "1", "--operation-id", str(uuid.uuid4()), *identity)
        self.assertEqual((entered["mode"], entered["version"]), ("maintenance", 2))
        aborted = run("abort", "--expected-version", "2", "--operation-id", str(uuid.uuid4()), *identity)
        self.assertEqual((aborted["mode"], aborted["version"]), ("open", 3))
