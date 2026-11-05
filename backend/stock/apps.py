from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class StockConfig(AppConfig):
    name = "stock"
    verbose_name = _("Stock")

    def ready(self):
        from django.db.backends.signals import connection_created
        from stock.services.stock_queries import register_stock_functions
        from stock.services.cost_constraints import register_cost_functions
        connection_created.connect(register_stock_functions, dispatch_uid="core.stock_functions")
        connection_created.connect(register_cost_functions, dispatch_uid="core.cost_functions")
