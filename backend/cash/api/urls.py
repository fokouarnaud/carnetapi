"""Routes owned by the cash domain; external API paths remain stable."""
from django.urls import include, path
from cash.api.cash import CashViewSet
from cash.api.cash_counts import CashCountViewSet

urlpatterns = [
    path("shops/<uuid:shop_id>/cash-counts/", CashCountViewSet.as_view({"get": "list", "post": "create"}), name="cash-count-list"),
    path("shops/<uuid:shop_id>/cash-counts/<uuid:pk>/", CashCountViewSet.as_view({"get": "retrieve"}), name="cash-count-detail"),
    path("shops/<uuid:shop_id>/cash/", CashViewSet.as_view({"get": "list", "post": "create"}), name="cash-list"),
]
