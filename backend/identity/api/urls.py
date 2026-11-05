"""Routes owned by the identity domain; external API paths remain stable."""
from django.urls import include, path
from identity.api.views import ShopViewSet
from identity.api.token_refresh import DeviceTokenRefreshView
from identity.api.team import TeamViewSet
from identity.api.language_preferences import LanguagePreferenceViewSet
from identity.api.restore_seller_access import RestoreSellerAccessView
from rest_framework.routers import SimpleRouter

router = SimpleRouter()
router.register("shops", ShopViewSet, basename="shop")

urlpatterns = [
    path("account/language/", LanguagePreferenceViewSet.as_view({"get": "retrieve", "patch": "partial_update"}), name="account-language"),
    path("shops/<uuid:shop_id>/team/", TeamViewSet.as_view({"get": "list"}), name="team-list"),
    path("shops/<uuid:shop_id>/team/<uuid:pk>/restore-access/",RestoreSellerAccessView.as_view(),name="team-restore-access"),
    path("shops/<uuid:shop_id>/team/<uuid:pk>/history/", TeamViewSet.as_view({"get": "history"}), name="team-history"),
    path("shops/<uuid:shop_id>/team/<uuid:pk>/", TeamViewSet.as_view({"get": "retrieve", "patch": "partial_update"}), name="team-detail"),
    path("", include(router.urls)),
    path("auth/refresh/", DeviceTokenRefreshView.as_view(), name="token-refresh"),
]
