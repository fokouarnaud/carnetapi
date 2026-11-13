"""Routes owned by the sync domain."""
from django.urls import path

from sync.api.views import ChangeFeedView

urlpatterns = [
    path("shops/<uuid:shop_id>/changes/", ChangeFeedView.as_view(), name="shop-changes"),
]
