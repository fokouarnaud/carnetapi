"""Read SMTP configuration without importing credentials into versioned source."""
import json
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured


EMAIL_KEYS = frozenset({"EMAIL_HOST", "EMAIL_PORT", "EMAIL_USE_TLS", "EMAIL_HOST_USER", "EMAIL_HOST_PASSWORD"})


def email_settings(environ, *, secret_path=None):
    values = {}
    path = secret_path or environ.get("APP_EMAIL_CONFIG_FILE")
    if path:
        try:
            values = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ImproperlyConfigured("Cannot read private SMTP configuration.") from None
        if not isinstance(values, dict) or set(values) - EMAIL_KEYS:
            raise ImproperlyConfigured("Invalid private SMTP configuration.")
    values = {**values, **{key: environ[key] for key in EMAIL_KEYS if key in environ}}
    host = values.get("EMAIL_HOST", "localhost")
    username = values.get("EMAIL_HOST_USER", "")
    password = values.get("EMAIL_HOST_PASSWORD", "")
    if any(type(value) is not str for value in (host, username, password)) or not host.strip():
        raise ImproperlyConfigured("Invalid SMTP connection fields.")
    try:
        port = int(str(values.get("EMAIL_PORT", "25")))
    except ValueError:
        raise ImproperlyConfigured("Invalid SMTP port.") from None
    if not 1 <= port <= 65535:
        raise ImproperlyConfigured("Invalid SMTP port.")
    tls = str(values.get("EMAIL_USE_TLS", "0")).lower()
    if tls not in {"0", "1", "true", "false"}:
        raise ImproperlyConfigured("Invalid SMTP TLS setting.")
    use_tls = tls in {"1", "true"}
    if bool(username) != bool(password):
        raise ImproperlyConfigured("Both SMTP authentication fields are required.")
    if username and not use_tls:
        raise ImproperlyConfigured("SMTP authentication requires TLS.")
    return {"EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend", "EMAIL_HOST": host.strip(),
        "EMAIL_PORT": port, "EMAIL_USE_TLS": use_tls, "EMAIL_USE_SSL": False,
        "EMAIL_HOST_USER": username, "EMAIL_HOST_PASSWORD": password, "EMAIL_TIMEOUT": 20,
        "DEFAULT_FROM_EMAIL": username or "webmaster@localhost", "SERVER_EMAIL": username or "root@localhost"}
