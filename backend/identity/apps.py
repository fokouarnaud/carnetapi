from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class IdentityConfig(AppConfig):
    name = "identity"
    verbose_name = _("Shop management")
