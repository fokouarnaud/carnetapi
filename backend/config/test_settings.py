"""Only for isolated tests, never a deployment configuration."""
import os

os.environ["APP_ENV"] = "test"
os.environ["APP_SECRET_KEY"] = "test-only-django-key-with-ample-length-not-for-production"
os.environ["APP_JWT_SIGNING_KEY"] = "test-only-jwt-signing-key-with-ample-length-not-for-production"
from .settings import *  # noqa: F403,E402

ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
# Tests normally use transient in-memory business databases. Dedicated barrier
# tests override this with a real, separately initialized gate and SQLite file.
RESTORE_GATE_PATH = None
OTP_SMS_TRANSPORT = "fake"
