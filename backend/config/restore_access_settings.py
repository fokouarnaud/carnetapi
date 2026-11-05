"""Console-only access review on an existing, explicitly selected database."""
import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured
from .technical_recovery_settings import *  # noqa: F403

_database = Path(os.environ.get("APP_RESTORE_ACCESS_DATABASE", ""))
if not _database.is_absolute() or _database.is_symlink() or not _database.is_file():
    raise ImproperlyConfigured("An existing access-review database is required.")
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3",
    "NAME": _database.resolve().as_uri()+"?mode=rw",
    "OPTIONS": {"uri": True, "timeout": 20, "transaction_mode": "IMMEDIATE"}}}
