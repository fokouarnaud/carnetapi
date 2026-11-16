"""Routes of the accountant's working exports (16 November 2026)."""
from django.urls import path

from accounting.api.cash_journal import CashJournalViewSet
from accounting.api.party_balances import PartyBalancesViewSet
from accounting.api.stock_valuation import StockExportViewSet
from accounting.api.sales_purchases_journal import SalesPurchasesJournalViewSet

urlpatterns = [
    path("shops/<uuid:shop_id>/accounting/cash-journal/", CashJournalViewSet.as_view({"get": "list"}),
         name="accounting-cash-journal"),
    path("shops/<uuid:shop_id>/accounting/sales-purchases-journal/",
         SalesPurchasesJournalViewSet.as_view({"get": "list"}), name="accounting-sales-purchases-journal"),
    path("shops/<uuid:shop_id>/accounting/balances/", PartyBalancesViewSet.as_view({"get": "list"}),
         name="accounting-balances"),
    path("shops/<uuid:shop_id>/accounting/stock/", StockExportViewSet.as_view({"get": "list"}),
         name="accounting-stock"),
]
