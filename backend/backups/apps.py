from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class BackupsConfig(AppConfig):
    name = "backups"
    verbose_name = _("Server backups")
