"""The trusted application graph used by the server and isolated restore worker."""
INSTALLED_APPLICATIONS = [
    "unfold", "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "rest_framework", "rest_framework_simplejwt.token_blacklist", "drf_spectacular",
    "core", "identity", "catalogue", "stock", "sales", "customers", "purchasing", "cash", "backups", "sync",
]
