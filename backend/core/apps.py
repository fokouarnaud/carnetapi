from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CoreConfig(AppConfig):
    name = "core"
    verbose_name = _("Technical foundation")

    def ready(self):
        from common.services import schema  # noqa: F401
