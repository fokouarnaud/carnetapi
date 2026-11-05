"""Deployment settings. Secrets come from environment or an external private file."""
import os
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from .mail import email_settings
from .applications import INSTALLED_APPLICATIONS
from .recovery import recovery_web_options

BASE_DIR = Path(__file__).resolve().parent.parent
APP_ENV = os.environ.get("APP_ENV", "production")
_private_smtp = Path.home() / ".config" / "app_x" / "smtp.json"
globals().update(email_settings(os.environ, secret_path=(
    _private_smtp if APP_ENV != "test" and "APP_EMAIL_CONFIG_FILE" not in os.environ and _private_smtp.is_file() else None)))
SECRET_KEY = os.environ.get("APP_SECRET_KEY")
JWT_SIGNING_KEY = os.environ.get("APP_JWT_SIGNING_KEY")
if not SECRET_KEY or not JWT_SIGNING_KEY or SECRET_KEY == JWT_SIGNING_KEY:
    raise ImproperlyConfigured("Set distinct APP_SECRET_KEY and APP_JWT_SIGNING_KEY.")

DEBUG = False
ALLOWED_HOSTS = os.environ.get("APP_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
INSTALLED_APPS = list(INSTALLED_APPLICATIONS)
RESTORE_RECOVERY = recovery_web_options(os.environ)
UNFOLD = {"LOGIN":{"form":"backups.recovery_forms.RecoveryAuthenticationForm"}}
# A direct Unfold login has no business profile page to return to.
LOGIN_REDIRECT_URL = "/admin/"
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "backups.middleware.RestoreGateMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "backups.uploads.RestoreUploadMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": os.environ.get("APP_DB_PATH", str(BASE_DIR / "db.sqlite3")),
    "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
}}
AUTH_USER_MODEL = "core.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "fr"
LANGUAGES = [("fr", "Français"), ("en", "English")]
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
BACKUP_ROOT = Path(os.environ.get("APP_BACKUP_ROOT", str(BASE_DIR / "backup-files")))
# External control state must survive restoration of the business database.
RESTORE_GATE_PATH = Path(os.environ.get("APP_RESTORE_GATE_PATH", str(BASE_DIR / "runtime-control" / "restore-gate.sqlite3")))
BACKUP_ENCRYPTION_KEY = os.environ.get("APP_BACKUP_ENCRYPTION_KEY", "")
BACKUP_KEY_ID = os.environ.get("APP_BACKUP_KEY_ID", "primary")
BACKUP_PART_MAX_BYTES = 15000000
BACKUP_SNAPSHOT_TIMEOUT_SECONDS = 30
BACKUP_EMAIL_MAX_BYTES = 25000000
BACKUP_SCHEDULER_TOKEN = os.environ.get("APP_BACKUP_SCHEDULER_TOKEN", "")
BACKUP_SCHEDULER_ACTOR_ID = os.environ.get("APP_BACKUP_SCHEDULER_ACTOR_ID", "")
HOSTING_REMINDER_TOKEN = os.environ.get("APP_HOSTING_REMINDER_TOKEN", "")
HOSTING_REMINDER_ACTOR_ID = os.environ.get("APP_HOSTING_REMINDER_ACTOR_ID", "")
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = APP_ENV == "production"
CSRF_COOKIE_SECURE = APP_ENV == "production"
SECURE_SSL_REDIRECT = APP_ENV == "production"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_HSTS_SECONDS = 31536000 if APP_ENV == "production" else 0

# Technical request bounds; no commercial quota or truncation of a command.
API_JSON_MAX_BYTES = 1000000
API_JSON_MAX_DEPTH = 64

REST_FRAMEWORK = {
    "DEFAULT_PARSER_CLASSES": [
        "common.services.parsers.BoundedJSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    "EXCEPTION_HANDLER": "common.services.exception_handler.localized_exception_handler",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "identity.services.authentication.DeviceJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "common.services.pagination.BoundedPagination",
    "PAGE_SIZE": 25,
}
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=5),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "SIGNING_KEY": JWT_SIGNING_KEY,
}
SPECTACULAR_SETTINGS = {
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        "common.services.schema_maintenance.describe_maintenance",
    ],
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {
        "ProductPriceConflictStateEnum": ["open", "resolved"],
        "PublicOpenProductPriceConflictStateEnum": ["open"],
        "MembershipRoleEnum": "identity.models.Membership.Role",
        "SupplierCreditKindEnum": "purchasing.models.SupplierCredit.Kind",
        "NonSellableDecisionKindEnum": "stock.models.NonSellableDecision.Kind",
        "ExactCostStateEnum": ["known", "provisional", "unknown"],
        "SaleInputModeEnum": ["cash","credit"],
        "RecoverySaleReviewStateEnum": ["pending","refused","validated"],
        "RecoveryReviewInputStateEnum": ["pending","refused"],
        "RestoredAccessDecisionStateEnum": ["confirmed","refused"],
        "RestoredAccessReviewStateEnum": ["pending","confirmed","refused"],
        "BackupTriggerStatusEnum": ["idle","disabled","blocked","busy","prepared","progressed","accepted","incomplete"],
        "BackupSchedulerErrorCodeEnum": ["backup_scheduler_unavailable"],
        "HostingReminderErrorCodeEnum": ["hosting_reminder_unavailable"],
        "OriginReferenceErrorCodeEnum": ["origin_reference_conflict"],
        "FrozenReceiptCostOriginKindEnum": ["frozen"],
        "ReturnCostOriginKindEnum": ["frozen", "reconstructed_legacy"],
    },
    "TITLE": "Boutique API (nom provisoire)",
    "DESCRIPTION": "Périmètre confirmé dans analysis/scope-v1.md. API en construction.",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SERVE_AUTHENTICATION": [
        "identity.services.authentication.DeviceJWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "SERVE_PERMISSIONS": ["identity.services.permissions.IsTechnicalSuperuser"],
}
