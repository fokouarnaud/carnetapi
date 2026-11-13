"""Routes owned by the identity domain; external API paths remain stable."""
from django.urls import include, path
from identity.api.views import ShopViewSet
from identity.api.token_refresh import DeviceTokenRefreshView
from identity.api.otp_login import OtpRequestView, OtpVerifyView, RecoveryCodesView
from identity.api.team import TeamViewSet
from identity.api.language_preferences import LanguagePreferenceViewSet
from identity.api.restore_seller_access import RestoreSellerAccessView
from rest_framework.routers import SimpleRouter
from identity.api.account_recovery import OwnershipTransferView, RecoveryCaseCompleteView, RecoveryCaseListView

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
    path("auth/otp/request/", OtpRequestView.as_view(), name="otp-request"),
    path("auth/otp/verify/", OtpVerifyView.as_view(), name="otp-verify"),
    path("account/recovery-codes/", RecoveryCodesView.as_view(), name="account-recovery-codes"),
    path("technical/recovery-cases/", RecoveryCaseListView.as_view(), name="recovery-case-list"),
    path("technical/shops/<uuid:shop_id>/ownership-transfer/", OwnershipTransferView.as_view(), name="ownership-transfer"),
    path("technical/recovery-cases/<uuid:pk>/complete/", RecoveryCaseCompleteView.as_view(), name="recovery-case-complete"),
]
