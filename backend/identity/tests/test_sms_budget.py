"""A persistent global SMS budget admits at most ten distinct sends per local day."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import uuid

from backend.identity.services.sms_budget import (
    SmsBudgetConflict, SmsBudgetStore, SmsBudgetUnavailable, SmsQuotaReached,
)


class SmsBudgetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "sms-budget.sqlite3"
        self.now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
        self.store = SmsBudgetStore(self.path, clock=lambda: self.now)
        self.operator = uuid.uuid4()

    def initialize(self, fresh=True):
        self.store.initialize(operator_id=self.operator, reason="Qualification", fresh_deployment=fresh)

    def reserve(self, operation=None, digest="1" * 64):
        return self.store.reserve(operation_id=operation or uuid.uuid4(), request_digest=digest)

    def test_absent_control_never_creates_a_new_empty_budget_implicitly(self):
        with self.assertRaises(SmsBudgetUnavailable):
            self.reserve()
        self.assertFalse(self.path.exists())

    def test_ten_reservations_survive_reopening_and_reject_the_eleventh(self):
        self.initialize()
        for ordinal in range(1, 11):
            receipt, created = self.reserve()
            self.assertTrue(created)
            self.assertEqual(receipt.ordinal, ordinal)
            self.assertEqual(receipt.local_date.isoformat(), "2026-11-06")
        reopened = SmsBudgetStore(self.path, clock=lambda: self.now)
        with self.assertRaises(SmsQuotaReached) as caught:
            reopened.reserve(operation_id=uuid.uuid4(), request_digest="2" * 64)
        self.assertEqual(caught.exception.next_allowed_at,
                         datetime(2026, 11, 6, 23, tzinfo=timezone.utc))
        self.assertEqual(reopened.status().remaining, 0)

    def test_exact_retry_has_the_same_receipt_but_never_a_second_send_permission(self):
        self.initialize()
        operation = uuid.uuid4()
        first, created = self.reserve(operation)
        again, replay_created = self.reserve(operation)
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(again, first)
        with self.assertRaises(SmsBudgetConflict):
            self.reserve(operation, "2" * 64)
        self.assertEqual(self.store.status().remaining, 9)

    def test_day_rolls_at_douala_midnight_without_reissuing_an_old_operation(self):
        self.initialize()
        self.now = datetime(2026, 11, 6, 22, 59, 59, tzinfo=timezone.utc)
        operation = uuid.uuid4()
        first, _ = self.reserve(operation)
        self.now += timedelta(seconds=1)
        replay, created = self.reserve(operation)
        self.assertFalse(created)
        self.assertEqual(replay, first)
        next_day, created = self.reserve()
        self.assertTrue(created)
        self.assertEqual(next_day.local_date.isoformat(), "2026-11-07")
        self.assertEqual(next_day.ordinal, 1)

    def test_concurrent_distinct_requests_admit_exactly_ten(self):
        self.initialize()
        barrier = threading.Barrier(12)

        def issue(_):
            concurrent = SmsBudgetStore(self.path, clock=lambda: self.now)
            barrier.wait(timeout=10)
            try:
                return concurrent.reserve(operation_id=uuid.uuid4(), request_digest="3" * 64)[1]
            except SmsQuotaReached:
                return False

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(issue, range(12)))
        self.assertEqual(sum(results), 10)
        self.assertEqual(self.store.status().remaining, 0)

    def test_concurrent_same_operation_grants_a_send_once(self):
        self.initialize()
        operation = uuid.uuid4()
        barrier = threading.Barrier(2)

        def issue(_):
            barrier.wait(timeout=10)
            return SmsBudgetStore(self.path, clock=lambda: self.now).reserve(
                operation_id=operation, request_digest="4" * 64)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(issue, range(2)))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(self.store.status().remaining, 9)

    def test_clock_rollback_cannot_reopen_an_earlier_daily_allowance(self):
        self.initialize()
        self.reserve()
        self.now += timedelta(days=1)
        self.reserve()
        self.now -= timedelta(days=1)
        with self.assertRaises(SmsBudgetUnavailable):
            self.reserve()

    def test_a_recovery_without_the_control_copy_blocks_until_next_local_day(self):
        self.initialize(fresh=False)
        self.assertEqual(self.store.status().remaining, 0)
        with self.assertRaises(SmsQuotaReached):
            self.reserve()
        self.now = datetime(2026, 11, 6, 23, tzinfo=timezone.utc)
        receipt, created = self.reserve()
        self.assertTrue(created)
        self.assertEqual(receipt.ordinal, 1)

    def test_initialization_never_overwrites_an_existing_control(self):
        self.initialize()
        self.reserve()
        before = self.path.read_bytes()
        with self.assertRaises(SmsBudgetConflict):
            self.initialize()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.status().remaining, 9)

    def test_unrelated_database_is_refused_without_opening_a_new_budget(self):
        with closing(sqlite3.connect(self.path)) as database, database:
            database.execute("CREATE TABLE business(value TEXT)")
            database.execute("INSERT INTO business VALUES ('preserved')")
        before = self.path.read_bytes()
        with self.assertRaises(SmsBudgetUnavailable):
            self.reserve()
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_failed_provider_call_does_not_release_the_reserved_slot(self):
        self.initialize()
        operation = uuid.uuid4()
        receipt, created = self.reserve(operation)
        self.assertTrue(created)
        # Transport can fail after the provider accepted the SMS. There is no release operation.
        reopened = SmsBudgetStore(self.path, clock=lambda: self.now)
        replay, created = reopened.reserve(operation_id=operation, request_digest="1" * 64)
        self.assertEqual(replay, receipt)
        self.assertFalse(created)
        self.assertEqual(reopened.status().remaining, 9)


if __name__ == "__main__":
    unittest.main()
