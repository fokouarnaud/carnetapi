"""Internal migration worker only: no application settings, live DB or SMTP secrets."""
import os
from pathlib import Path
import secrets
from django.core.exceptions import ImproperlyConfigured
from .applications import INSTALLED_APPLICATIONS

_work = Path(os.environ.get("APP_RESTORE_WORKDIR", ""))
if (not _work.is_absolute() or not _work.name.startswith("restore-work-") or _work.is_symlink()
        or not (_work / "request.json").is_file() or not (_work / "candidate.sqlite3").is_file()):
    raise ImproperlyConfigured("An isolated restore work directory is required.")

SECRET_KEY = secrets.token_urlsafe(48)
INSTALLED_APPS = list(INSTALLED_APPLICATIONS)
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(_work / "candidate.sqlite3"),
    "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"}}}
AUTH_USER_MODEL = "core.User"
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
MIDDLEWARE = []
ALLOWED_HOSTS = []
DEBUG = False
EMAIL_BACKEND = "config.restore_settings.DisabledMailBackend"


class DisabledMailBackend:
    def __init__(self, *args, **kwargs):
        raise ImproperlyConfigured("Email is disabled during restore preparation.")
