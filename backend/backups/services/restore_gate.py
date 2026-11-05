"""External, process-shared maintenance barrier; never restores archived rights.

Every participating worker must hold its request lease until all database work
finishes. Exclusive transitions drain these leases before changing the mode.
This primitive does not replace a database or authorize a technical account.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid


class RestoreGateError(Exception):
    """A gate cannot safely authorize access."""


class RestoreGateBusy(RestoreGateError):
    """An active request or controller still holds the gate."""


class RestoreGateClosed(RestoreGateError):
    """Maintenance excludes business requests."""


class RestoreGateConflict(RestoreGateError):
    """The operation or expected version differs from the recorded state."""


def _identity(operation_id, requested_by, reason):
    try:
        operation, actor = str(uuid.UUID(str(operation_id))), str(uuid.UUID(str(requested_by)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RestoreGateError("restore_gate_invalid_identity") from exc
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise RestoreGateError("restore_gate_invalid_reason")
    return operation, actor, reason.strip()


def _path(path):
    value = Path(path)
    if value.is_symlink():
        raise RestoreGateError("restore_gate_symlink")
    return value.resolve()


@contextmanager
def _connection(path, timeout=0):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout <= 60:
        raise RestoreGateError("restore_gate_invalid_timeout")
    target = _path(path)
    db = None
    try:
        db = sqlite3.connect(target.as_uri() + "?mode=rw", uri=True, timeout=timeout)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        version=db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (1,2):
            raise RestoreGateError("restore_gate_invalid_format")
        if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise RestoreGateError("restore_gate_invalid_journal")
        if version==2:
            db.execute("SELECT operation_id,state,gate_version,request,receipt FROM gate_installations LIMIT 0")
        yield db
    except sqlite3.Error as exc:
        if getattr(exc, "sqlite_errorcode", 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise RestoreGateBusy("restore_gate_busy") from exc
        raise RestoreGateError("restore_gate_unavailable") from exc
    finally:
        if db is not None:
            db.close()


def _state(db):
    row = db.execute("SELECT * FROM gate_state WHERE singleton=1").fetchone()
    if row is None or row["mode"] not in ("open", "maintenance"):
        raise RestoreGateError("restore_gate_invalid_state")
    return dict(row)


def _create_installations(db):
    db.execute("""CREATE TABLE gate_installations (
        operation_id TEXT PRIMARY KEY NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('reserved','installed')),
        gate_version INTEGER NOT NULL UNIQUE CHECK(typeof(gate_version)='integer' AND gate_version>0),
        request TEXT NOT NULL CHECK(json_valid(request) AND length(request)<=100000),
        receipt TEXT,
        CHECK((state='reserved' AND receipt IS NULL) OR
              (state='installed' AND receipt IS NOT NULL AND json_valid(receipt) AND length(receipt)<=100000)))""")
    db.execute("CREATE UNIQUE INDEX one_pending_installation ON gate_installations(state) WHERE state='reserved'")


def _installation(db, *, version=None):
    if db.execute("PRAGMA user_version").fetchone()[0]!=2:
        return None
    if version is None:
        return db.execute("SELECT operation_id,state FROM gate_installations WHERE state='reserved' LIMIT 1").fetchone()
    return db.execute("SELECT operation_id,state FROM gate_installations WHERE gate_version=?",[version]).fetchone()


def _require_no_installation(db):
    if _installation(db) is not None:
        raise RestoreGateClosed("restore_installation_incomplete")


def _fingerprint(path,*,allow_missing=False):
    result = {}
    for suffix in ("", "-wal", "-journal"):
        target = _path(str(path) + suffix)
        if not target.exists():
            result[suffix] = None
            continue
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        result[suffix] = digest.hexdigest()
    if result[""] is None and not allow_missing:
        raise RestoreGateError("restore_gate_database_missing")
    return json.dumps(result, sort_keys=True)


def _event(db, state, operation, actor, reason, action, expected):
    receipt = {"operation_id": operation, "requested_by": actor, "reason": reason,
               "action": action, "expected_version": expected,
               "mode": state["mode"], "version": state["version"],
               "recorded_at": datetime.now(timezone.utc).isoformat()}
    db.execute("INSERT INTO gate_events VALUES (?, ?)", (operation, json.dumps(receipt, sort_keys=True)))
    return receipt


def initialize_gate(gate_path, database_path, *, operation_id, requested_by, reason):
    """Explicitly install a new external gate without overwriting an existing one."""
    operation, actor, reason = _identity(operation_id, requested_by, reason)
    gate, business = _path(gate_path), _path(database_path)
    if gate == business or not business.is_file():
        raise RestoreGateError("restore_gate_invalid_database")
    try:
        descriptor = os.open(gate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        raise RestoreGateError("restore_gate_creation_refused") from exc
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(gate)) as db, db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA user_version=2")
            db.execute("""CREATE TABLE gate_events (
                operation_id TEXT PRIMARY KEY NOT NULL, receipt TEXT NOT NULL)""")
            db.execute("""CREATE TABLE gate_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), database_path TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN ('open','maintenance')),
                version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version>0),
                fingerprint TEXT,
                CHECK((mode='open' AND fingerprint IS NULL) OR
                      (mode='maintenance' AND fingerprint IS NOT NULL)))""")
            db.execute("INSERT INTO gate_state VALUES (1, ?, 'open', 1, NULL)", (str(business),))
            _create_installations(db)
            receipt = _event(db, {"mode": "open", "version": 1}, operation, actor, reason, "initialize", 0)
        return receipt
    except (OSError, sqlite3.Error) as exc:
        raise RestoreGateError("restore_gate_creation_failed") from exc
    # An interrupted initializer intentionally leaves an invalid, closed gate.


def read_gate(gate_path):
    """Read durable state without opening the business database."""
    with _connection(gate_path) as db:
        state=_state(db)
        state["gate_format"]=db.execute("PRAGMA user_version").fetchone()[0]
        installation=_installation(db,version=state["version"])
        if installation is not None:
            state["installation"]=dict(installation)
        return state


def upgrade_gate(gate_path,database_path,*,operation_id,requested_by,reason,expected_version,timeout=0):
    """Explicitly extend a legacy external gate; preserve its mode and audit."""
    operation,actor,reason=_identity(operation_id,requested_by,reason)
    if type(expected_version) is not int or expected_version<1:
        raise RestoreGateConflict("restore_gate_state_conflict")
    with _connection(gate_path,timeout) as db:
        db.execute("BEGIN EXCLUSIVE")
        state=_state(db)
        if state["database_path"]!=str(_path(database_path)):
            raise RestoreGateConflict("restore_gate_state_conflict")
        prior=db.execute("SELECT receipt FROM gate_events WHERE operation_id=?",[operation]).fetchone()
        if prior is not None:
            receipt=json.loads(prior[0])
            if any(receipt[key]!=value for key,value in (("action","upgrade"),("requested_by",actor),
                    ("reason",reason),("expected_version",expected_version))):
                raise RestoreGateConflict("restore_gate_operation_conflict")
            return receipt
        if state["version"]!=expected_version or db.execute("PRAGMA user_version").fetchone()[0]!=1:
            raise RestoreGateConflict("restore_gate_state_conflict")
        _create_installations(db)
        db.execute("PRAGMA user_version=2")
        state["version"]+=1
        db.execute("UPDATE gate_state SET version=? WHERE singleton=1",[state["version"]])
        receipt=_event(db,state,operation,actor,reason,"upgrade",expected_version)
        db.commit()
        return receipt


@contextmanager
def hold_control(gate_path, database_path, *, required_mode, expected_version, timeout=0):
    """Exclude requests and controllers while a console action validates its state.

    This lease changes no state. It prevents a pre-incident provision or a
    maintenance operation from racing a gate transition or database replacement.
    """
    if required_mode not in ("open", "maintenance") or type(expected_version) is not int or expected_version < 1:
        raise RestoreGateConflict("restore_gate_state_conflict")
    with _connection(gate_path, timeout) as db:
        db.execute("BEGIN EXCLUSIVE")
        state = _state(db)
        _require_no_installation(db)
        if (state["mode"] != required_mode or state["version"] != expected_version
                or state["database_path"] != str(_path(database_path))):
            raise RestoreGateConflict("restore_gate_state_conflict")
        yield state


class _RequestLease:
    def __init__(self, connection_context, connection):
        self.context = connection_context
        self.connection = connection

    def __enter__(self):
        return self

    def close(self):
        if self.connection is not None:
            try:
                self.connection.rollback()
            finally:
                self.connection = None
                self.context.__exit__(None, None, None)

    def __exit__(self, *exception):
        self.close()


def acquire_request(gate_path, database_path):
    """Hold a cross-process read lock until the returned lease is closed."""
    context = _connection(gate_path)
    db = context.__enter__()
    try:
        db.execute("BEGIN")
        state = _state(db)
        _require_no_installation(db)
        if state["database_path"] != str(_path(database_path)):
            raise RestoreGateError("restore_gate_wrong_database")
        if not _path(database_path).is_file():
            raise RestoreGateError("restore_gate_database_missing")
        if state["mode"] != "open":
            raise RestoreGateClosed("restore_gate_closed")
        return _RequestLease(context, db)
    except BaseException:
        context.__exit__(*sys.exc_info())
        raise


def acquire_recovery_request(gate_path,database_path,*,expected_version):
    """Keep maintenance in force while an explicitly enabled recovery response runs."""
    if type(expected_version) is not int or expected_version<1:
        raise RestoreGateConflict("restore_gate_invalid_version")
    context=_connection(gate_path)
    db=context.__enter__()
    try:
        db.execute("BEGIN")
        state=_state(db)
        _require_no_installation(db)
        if (state["database_path"]!=str(_path(database_path)) or state["mode"]!="maintenance"
                or state["version"]!=expected_version):
            raise RestoreGateConflict("restore_gate_state_conflict")
        return _RequestLease(context,db)
    except BaseException:
        context.__exit__(*sys.exc_info())
        raise


def _transition(gate_path, *, operation_id, requested_by, reason, expected_version, timeout, action):
    operation, actor, reason = _identity(operation_id, requested_by, reason)
    if type(expected_version) is not int or expected_version < 1:
        raise RestoreGateConflict("restore_gate_invalid_version")
    with _connection(gate_path, timeout) as db:
        db.execute("BEGIN EXCLUSIVE")
        previous = db.execute("SELECT receipt FROM gate_events WHERE operation_id=?", (operation,)).fetchone()
        if previous:
            receipt = json.loads(previous[0])
            if any(receipt[key] != value for key, value in (
                ("requested_by", actor), ("reason", reason), ("action", action),
                ("expected_version", expected_version))):
                raise RestoreGateConflict("restore_gate_operation_conflict")
            return receipt
        state = _state(db)
        _require_no_installation(db)
        if action=="abort" and _installation(db,version=state["version"]) is not None:
            raise RestoreGateClosed("restore_installation_requires_activation")
        required = "open" if action == "enter" else "maintenance"
        if state["mode"] != required or state["version"] != expected_version:
            raise RestoreGateConflict("restore_gate_state_conflict")
        fingerprint = _fingerprint(state["database_path"],allow_missing=action=="enter")
        if action == "abort" and fingerprint != state["fingerprint"]:
            raise RestoreGateError("restore_gate_database_changed")
        state.update(mode="maintenance" if action == "enter" else "open", version=expected_version + 1)
        db.execute("UPDATE gate_state SET mode=?, version=?, fingerprint=? WHERE singleton=1",
                   (state["mode"], state["version"], fingerprint if action == "enter" else None))
        receipt = _event(db, state, operation, actor, reason, action, expected_version)
        db.commit()
        return receipt


def enter_maintenance(gate_path, *, operation_id, requested_by, reason, expected_version, timeout=0):
    """Drain participating requests and persist maintenance before replacement."""
    return _transition(gate_path, operation_id=operation_id, requested_by=requested_by,
                       reason=reason, expected_version=expected_version, timeout=timeout, action="enter")


def abort_maintenance(gate_path, *, operation_id, requested_by, reason, expected_version, timeout=0):
    """Cancel maintenance only while business bytes and sidecars are unchanged."""
    return _transition(gate_path, operation_id=operation_id, requested_by=requested_by,
                       reason=reason, expected_version=expected_version, timeout=timeout, action="abort")
