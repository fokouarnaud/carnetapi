"""Console-only recovery: no application secrets, HTTP, mail, or default database."""
import secrets
from .applications import INSTALLED_APPLICATIONS

SECRET_KEY = secrets.token_urlsafe(48)
INSTALLED_APPS = list(INSTALLED_APPLICATIONS)
DATABASES = {"default": {"ENGINE": "django.db.backends.dummy"}}
AUTH_USER_MODEL = "core.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.PBKDF2PasswordHasher"]
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "fr"
LANGUAGES = [("fr", "Français"), ("en", "English")]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
MIDDLEWARE = []
ALLOWED_HOSTS = []
DEBUG = False
STATIC_URL = "static/"
EMAIL_BACKEND = "config.technical_recovery_settings.DisabledMailBackend"


class DisabledMailBackend:
    def __init__(self, *args, **kwargs):
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured("Mail is disabled during technical recovery.")
