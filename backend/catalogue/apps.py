from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CatalogueConfig(AppConfig):
    name = "catalogue"
    verbose_name = _("Catalogue")

    def ready(self):
        from django.db.backends.signals import connection_created
        from catalogue.api.catalogue_queries import register_catalogue_functions
        connection_created.connect(register_catalogue_functions, dispatch_uid="core.catalogue_functions")
