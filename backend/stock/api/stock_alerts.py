"""Stock alert centre, using the authorized catalogue representation."""
from catalogue.api.catalogue import ProductViewSet
from catalogue.models import Product
from common.services.operations import require_manager


class StockAlertViewSet(ProductViewSet):
    """Shop replenishment list; sellers use the ordinary product indicator."""

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Product.objects.none()
        require_manager(self.get_shop(), self.request.user)
        return super().get_queryset().filter(
            is_active=True, stock_threshold_milli__isnull=False,
            stock_low=True,
        )
