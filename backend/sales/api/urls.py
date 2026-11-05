"""Routes owned by the sales domain; external API paths remain stable."""
from django.urls import include, path
from sales.api.sales import SaleViewSet
from sales.api.sale_cancellations import SaleCancellationViewSet
from sales.api.sale_returns import SaleReturnViewSet

urlpatterns = [
    path("shops/<uuid:shop_id>/sales/<uuid:sale_id>/cancellation/", SaleCancellationViewSet.as_view({"get": "retrieve", "post": "create"}), name="sale-cancellation"),
    path("shops/<uuid:shop_id>/sales/<uuid:sale_id>/returns/", SaleReturnViewSet.as_view({"get": "list", "post": "create"}), name="sale-returns"),
    path("shops/<uuid:shop_id>/sales/<uuid:sale_id>/returns/<uuid:pk>/", SaleReturnViewSet.as_view({"get": "retrieve"}), name="sale-return"),
    path("shops/<uuid:shop_id>/sales/<uuid:pk>/history/", SaleViewSet.as_view({"get": "history"}), name="sale-history"),
    path("shops/<uuid:shop_id>/sales/", SaleViewSet.as_view({"get": "list", "post": "create"}), name="sale-list"),
    path("shops/<uuid:shop_id>/sales/<uuid:pk>/", SaleViewSet.as_view({"get": "retrieve"}), name="sale-detail"),
]
