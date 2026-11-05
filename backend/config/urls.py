"""Project entry points; domain apps own their API routes."""
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/v1/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger"),
    path("api/v1/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    path("api/v1/", include("identity.api.urls")),
    path("api/v1/", include("catalogue.api.urls")),
    path("api/v1/", include("stock.api.urls")),
    path("api/v1/", include("sales.api.urls")),
    path("api/v1/", include("customers.api.urls")),
    path("api/v1/", include("purchasing.api.urls")),
    path("api/v1/", include("cash.api.urls")),
    path("api/v1/", include("backups.api.urls")),
]
