from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CashConfig(AppConfig):
    name = "cash"
    verbose_name = _("Cash management")
