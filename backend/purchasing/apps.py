from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PurchasingConfig(AppConfig):
    name = "purchasing"
    verbose_name = _("Suppliers and purchases")

    def ready(self):
        from django.db.backends.signals import connection_created
        from purchasing.services.supplier_ledger import register_supplier_ledger_functions
        connection_created.connect(register_supplier_ledger_functions, dispatch_uid="core.supplier_ledger_functions")
