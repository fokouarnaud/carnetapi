from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class SyncConfig(AppConfig):
    name = "sync"
    verbose_name = _("Synchronisation")

    def ready(self):
        from sync import receivers  # noqa: F401
