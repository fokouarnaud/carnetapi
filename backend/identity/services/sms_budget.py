"""External durable admission control for the pilot's global SMS budget."""
from dataclasses import dataclass
from contextlib import closing, contextmanager
from datetime import date, datetime, time, timedelta, timezone
import os
from pathlib import Path
import re
import sqlite3
import uuid
from zoneinfo import ZoneInfo


DOUALA = ZoneInfo("Africa/Douala")
APPLICATION_ID = 0x534D5331
SCHEMA_VERSION = 1
DAILY_LIMIT = 10


class SmsBudgetUnavailable(Exception):
    """The counter cannot safely authorize a new provider invocation."""


class SmsBudgetConflict(Exception):
    """An operation identifier or control file already denotes different data."""


class SmsQuotaReached(Exception):
    def __init__(self, next_allowed_at):
        self.next_allowed_at = next_allowed_at
        super().__init__("sms_daily_quota_reached")


@dataclass(frozen=True)
class SmsBudgetReceipt:
    operation_id: uuid.UUID
    local_date: date
    ordinal: int
    reserved_at: datetime


@dataclass(frozen=True)
class SmsBudgetStatus:
    local_date: date
    remaining: int
    next_reset_at: datetime


class SmsBudgetStore:
    def __init__(self, path, *, clock=None):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("sms_budget_path_must_be_absolute")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def initialize(self, *, operator_id, reason, fresh_deployment):
        """Create a private control once; missing recovery history blocks the first day."""
        operator = str(uuid.UUID(str(operator_id)))
        if (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000
                or type(fresh_deployment) is not bool):
            raise ValueError("sms_budget_initialization_invalid")
        self._paths()
        now = self._now()
        day = now.astimezone(DOUALA).date()
        blocked_before = day if fresh_deployment else day + timedelta(days=1)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise SmsBudgetConflict("sms_budget_already_initialized") from None
        except OSError:
            raise SmsBudgetUnavailable("sms_budget_unavailable") from None
        os.close(descriptor)
        try:
            with closing(sqlite3.connect(self.path, timeout=20, isolation_level=None)) as database:
                database.execute("PRAGMA journal_mode=WAL")
                database.execute("PRAGMA synchronous=FULL")
                database.executescript("""
                    BEGIN EXCLUSIVE;
                    CREATE TABLE sms_budget_state (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        initialized_at TEXT NOT NULL,
                        operator_id TEXT NOT NULL,
                        reason TEXT NOT NULL CHECK(length(trim(reason))>0),
                        blocked_before_day TEXT NOT NULL,
                        last_observed_utc TEXT NOT NULL
                    ) STRICT;
                    CREATE TABLE sms_send_reservation (
                        operation_id TEXT PRIMARY KEY NOT NULL,
                        request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
                        local_date TEXT NOT NULL CHECK(length(local_date)=10),
                        ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND 10),
                        reserved_at TEXT NOT NULL,
                        UNIQUE(local_date,ordinal)
                    ) STRICT;
                    CREATE TRIGGER sms_reservation_no_update BEFORE UPDATE ON sms_send_reservation
                    BEGIN SELECT RAISE(ABORT,'sms reservation is immutable'); END;
                    CREATE TRIGGER sms_reservation_no_delete BEFORE DELETE ON sms_send_reservation
                    BEGIN SELECT RAISE(ABORT,'sms reservation is immutable'); END;
                """)
                stamp = now.isoformat(timespec="microseconds")
                database.execute("INSERT INTO sms_budget_state VALUES (1,?,?,?,?,?)",
                                 (stamp, operator, reason.strip(), blocked_before.isoformat(), stamp))
                database.execute(f"PRAGMA application_id={APPLICATION_ID}")
                database.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                database.commit()
                database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except (OSError, sqlite3.Error):
            # Leave an interrupted initializer closed; never reset it automatically.
            raise SmsBudgetUnavailable("sms_budget_initialization_failed") from None

    def _paths(self):
        if not self.path.parent.is_dir():
            raise SmsBudgetUnavailable("sms_budget_unavailable")
        for path in (self.path, *self.path.parents):
            if path.is_symlink() or path.is_junction():
                raise SmsBudgetUnavailable("sms_budget_unavailable")

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise SmsBudgetUnavailable("sms_budget_clock_unavailable")
        return value.astimezone(timezone.utc)

    @contextmanager
    def _database(self, *, write):
        database = None
        try:
            self._paths()
            with self.path.open("rb") as stream:
                header = stream.read(100)
            if (len(header) != 100 or header[:16] != b"SQLite format 3\x00"
                    or int.from_bytes(header[68:72], "big") != APPLICATION_ID
                    or int.from_bytes(header[60:64], "big") != SCHEMA_VERSION):
                raise SmsBudgetUnavailable("sms_budget_unavailable")
            database = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                       timeout=20, isolation_level=None)
            database.row_factory = sqlite3.Row
            database.execute("PRAGMA synchronous=FULL")
            database.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield database
            database.commit() if write else database.rollback()
        except (OSError, sqlite3.Error):
            raise SmsBudgetUnavailable("sms_budget_unavailable") from None
        finally:
            if database is not None:
                database.rollback()
                database.close()

    @staticmethod
    def _reset_at(day):
        return datetime.combine(day + timedelta(days=1), time(), tzinfo=DOUALA).astimezone(timezone.utc)

    def _window(self, database, now):
        state = database.execute("SELECT last_observed_utc,blocked_before_day FROM sms_budget_state WHERE singleton=1").fetchone()
        if state is None or now.isoformat(timespec="microseconds") < state["last_observed_utc"]:
            raise SmsBudgetUnavailable("sms_budget_clock_unavailable")
        day = now.astimezone(DOUALA).date()
        used = database.execute("SELECT COUNT(*) FROM sms_send_reservation WHERE local_date=?",
                                (day.isoformat(),)).fetchone()[0]
        return day, 0 if day.isoformat() < state["blocked_before_day"] else DAILY_LIMIT - used

    @staticmethod
    def _receipt(row):
        try:
            result = SmsBudgetReceipt(uuid.UUID(row["operation_id"]), date.fromisoformat(row["local_date"]),
                                      row["ordinal"], datetime.fromisoformat(row["reserved_at"]))
            if result.reserved_at.tzinfo is None:
                raise ValueError()
            return result
        except (ValueError, TypeError):
            raise SmsBudgetUnavailable("sms_budget_unavailable") from None

    def reserve(self, *, operation_id, request_digest):
        """Return the immutable receipt and whether ONE provider invocation may begin.

        A false second value never permits a resend, including after a timeout.
        A failed provider request keeps its slot; explicit resends need a new UUID.
        The caller must enforce authentication and canonicalize its request first.
        """
        identifier = str(uuid.UUID(str(operation_id)))
        if not isinstance(request_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", request_digest):
            raise ValueError("sms_request_digest_invalid")
        with self._database(write=True) as database:
            original = database.execute("SELECT * FROM sms_send_reservation WHERE operation_id=?", (identifier,)).fetchone()
            if original is not None:
                if original["request_digest"] != request_digest:
                    raise SmsBudgetConflict("sms_operation_conflict")
                return self._receipt(original), False
            now = self._now()
            day, remaining = self._window(database, now)
            if remaining <= 0:
                raise SmsQuotaReached(self._reset_at(day))
            ordinal = DAILY_LIMIT - remaining + 1
            stamp = now.isoformat(timespec="microseconds")
            database.execute("INSERT INTO sms_send_reservation VALUES (?,?,?,?,?)",
                             (identifier, request_digest, day.isoformat(), ordinal, stamp))
            database.execute("UPDATE sms_budget_state SET last_observed_utc=? WHERE singleton=1", (stamp,))
            return SmsBudgetReceipt(uuid.UUID(identifier), day, ordinal, now), True

    def status(self):
        """Read the remaining admission slots without mutating any reservation."""
        with self._database(write=False) as database:
            day, remaining = self._window(database, self._now())
            return SmsBudgetStatus(day, remaining, self._reset_at(day))
