"""Keep one synchronous database job inside the external restoration barrier."""
from contextlib import contextmanager

from django.conf import settings
from django.db import connections

from .restore_gate import acquire_request, RestoreGateError


@contextmanager
def normal_database_work():
    """Drain the job and close its ORM connections before releasing maintenance.

    Enter before the first query, outside any transaction, and leave after all
    SQL and external effects finish. This is a job boundary, not a per-query
    wrapper; it must not return lazy querysets or launch detached tasks.
    """
    path=settings.RESTORE_GATE_PATH
    if path is None:  # Explicit isolated-test configuration, as in HTTP.
        yield
        return
    database=connections["default"]
    if database.in_atomic_block or database.settings_dict["ENGINE"]!="django.db.backends.sqlite3":
        raise RestoreGateError("restore_worker_database_invalid")
    lease=acquire_request(path,database.settings_dict["NAME"])
    try:
        yield
    finally:
        try:
            connections.close_all()
        finally:
            lease.close()
