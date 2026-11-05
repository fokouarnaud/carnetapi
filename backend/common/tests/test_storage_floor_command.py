"""The storage floor is an isolated SQL measurement, never a pilot-load pass."""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from django.test import SimpleTestCase


class StorageFloorCommandTests(SimpleTestCase):
    def test_default_two_shop_pilot_is_measured_without_live_database_or_conflating_gates(self):
        repository = Path(__file__).resolve().parents[3]
        with TemporaryDirectory(prefix="storage-floor-command-") as directory:
            root = Path(directory)
            output, trap = root / "report.json", root / "live-must-not-exist.sqlite3"
            completed = subprocess.run([sys.executable, "-I", "-X", "utf8", str(repository / "tools/benchmark_backend_storage.py"),
                "--output", str(output), "--products-per-shop", "5", "--devices-per-shop", "1",
                "--sales-per-shop-day", "2", "--lines-per-sale", "5", "--days", "1", "--max-database-bytes", "100000000"],
                env={**os.environ, "DJANGO_SETTINGS_MODULE": "broken_settings", "APP_DB_PATH": str(trap)},
                capture_output=True, text=True, timeout=180)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["actual_counts"], {"shops": 2, "products": 10, "devices": 2, "sales": 4, "lines": 20, "activities": 4})
            self.assertTrue(report["target_row_counts_reached"])
            self.assertFalse(report["qualifies_pilot_load"])
            self.assertEqual(report["initial_budget_comparison"], "not_applicable_to_annual_dataset")
            self.assertEqual(report["annual_floor_status"], "host_limit_unknown")
            self.assertTrue(report["foreign_keys_valid"])
            self.assertTrue(report["integrity_valid"])
            self.assertTrue(report["sale_totals_match_lines"])
            self.assertEqual(report["database_bytes"], report["page_count"]*report["page_size"])
            self.assertTrue(report["temporary_database_removed"])
            self.assertFalse(trap.exists())

    def test_invalid_dimensions_fail_before_a_database_or_report_is_created(self):
        repository = Path(__file__).resolve().parents[3]
        with TemporaryDirectory(prefix="storage-floor-invalid-") as directory:
            output = Path(directory) / "report.json"
            completed = subprocess.run([sys.executable, "-I", "-X", "utf8", str(repository / "tools/benchmark_backend_storage.py"),
                "--output", str(output), "--shops", "0"], text=True, capture_output=True, timeout=20)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("invalid_storage_workload", completed.stderr)
            self.assertFalse(output.exists())
