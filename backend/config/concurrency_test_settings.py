"""File-backed SQLite for real locking checks, separate from the fast test suite."""
from .test_settings import *  # noqa: F403

DATABASES["default"]["TEST"] = {"NAME": str(BASE_DIR / ".concurrency-tests.sqlite3")}  # noqa: F405
