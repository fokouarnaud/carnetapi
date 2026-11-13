"""Routes owned by the stock domain; external API paths remain stable."""
from django.urls import include, path
from stock.api.stock_alerts import StockAlertViewSet
from stock.api.return_receipts import ReturnReceiptViewSet
from stock.api.unsellable_decisions import NonSellableDecisionViewSet
from stock.api.unsellable_stock import NonSellableStockViewSet
from stock.api.cost_revaluation_api import CostRevaluationViewSet
from stock.api.stock_losses import StockLossViewSet
from stock.api.stock_counts import StockCountViewSet
from stock.api.stock_valuations import StockValuationViewSet
from stock.api.sale_cost_issues import SaleCostIssueViewSet
from stock.api.stock_shortage_reviews import StockShortageReviewViewSet
from stock.api.stock_notifications import StockAlertEventViewSet, StockNotificationPreferenceViewSet
from stock.api.transfers import StockTransferViewSet, StockTransferReceiptViewSet
from stock.api.presence_releases import PresenceReleaseView
from stock.api.transfer_closures import TransferLineClosureView

urlpatterns = [
    path("shops/<uuid:shop_id>/transfers/<uuid:transfer_id>/lines/<uuid:line_id>/close/", TransferLineClosureView.as_view(), name="stock-transfer-line-close"),
    path("shops/<uuid:shop_id>/reservations/<uuid:reservation_id>/presence-release/", PresenceReleaseView.as_view(), name="reservation-presence-release"),
    path("shops/<uuid:shop_id>/transfers/",StockTransferViewSet.as_view({"get":"list","post":"create"}),name="stock-transfer-list"),
    path("shops/<uuid:shop_id>/transfers/<uuid:pk>/",StockTransferViewSet.as_view({"get":"retrieve"}),name="stock-transfer-detail"),
    path("shops/<uuid:shop_id>/transfers/<uuid:pk>/receipts/",StockTransferReceiptViewSet.as_view({"get":"list","post":"create"}),name="stock-transfer-receipts"),
    path("shops/<uuid:shop_id>/sale-cost-issues/", SaleCostIssueViewSet.as_view({"get": "list"}), name="shop-sale-cost-issues"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/sale-cost-issues/", SaleCostIssueViewSet.as_view({"get": "list"}), name="sale-cost-issues"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/stock-shortage-reviews/preview/", StockShortageReviewViewSet.as_view({"post": "preview"}), name="stock-shortage-review-preview"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/stock-shortage-reviews/movements/", StockShortageReviewViewSet.as_view({"get": "movements"}), name="stock-shortage-movements"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/stock-shortage-reviews/", StockShortageReviewViewSet.as_view({"get": "list", "post": "create"}), name="stock-shortage-reviews"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/stock-shortage-reviews/<uuid:pk>/", StockShortageReviewViewSet.as_view({"get": "retrieve"}), name="stock-shortage-review-detail"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/stock-valuations/preview/", StockValuationViewSet.as_view({"post": "preview"}), name="stock-valuation-preview"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/stock-valuations/current/", StockValuationViewSet.as_view({"get": "current"}), name="stock-valuation-current"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/stock-valuations/", StockValuationViewSet.as_view({"get": "list", "post": "create"}), name="stock-valuations"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/stock-valuations/<uuid:pk>/", StockValuationViewSet.as_view({"get": "retrieve"}), name="stock-valuation-detail"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/cost-revaluations/preview/", CostRevaluationViewSet.as_view({"get": "preview"}), name="cost-revaluation-preview"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/cost-revaluations/", CostRevaluationViewSet.as_view({"get": "list", "post": "create"}), name="cost-revaluations"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/cost-revaluations/<uuid:pk>/", CostRevaluationViewSet.as_view({"get": "retrieve"}), name="cost-revaluation"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/cost-revaluations/<uuid:pk>/items/", CostRevaluationViewSet.as_view({"get": "items"}), name="cost-revaluation-items"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/cost-revaluations/<uuid:pk>/returns/", CostRevaluationViewSet.as_view({"get": "returns"}), name="cost-revaluation-returns"),
    path("shops/<uuid:shop_id>/unsellable-stock/", NonSellableStockViewSet.as_view({"get": "list"}), name="unsellable-stock"),
    path("shops/<uuid:shop_id>/sales/<uuid:sale_id>/returns/<uuid:return_id>/lines/<uuid:line_id>/receipts/<uuid:receipt_id>/decisions/", NonSellableDecisionViewSet.as_view({"get": "list", "post": "create"}), name="unsellable-decisions"),
    path("shops/<uuid:shop_id>/sales/<uuid:sale_id>/returns/<uuid:return_id>/lines/<uuid:line_id>/receipts/<uuid:receipt_id>/decisions/<uuid:pk>/", NonSellableDecisionViewSet.as_view({"get": "retrieve"}), name="unsellable-decision"),
    path("shops/<uuid:shop_id>/sales/<uuid:sale_id>/returns/<uuid:return_id>/lines/<uuid:line_id>/receipts/", ReturnReceiptViewSet.as_view({"get": "list", "post": "create"}), name="return-receipts"),
    path("shops/<uuid:shop_id>/sales/<uuid:sale_id>/returns/<uuid:return_id>/lines/<uuid:line_id>/receipts/<uuid:pk>/", ReturnReceiptViewSet.as_view({"get": "retrieve"}), name="return-receipt"),
    path("shops/<uuid:shop_id>/stock-alert-events/", StockAlertEventViewSet.as_view({"get": "list"}), name="stock-alert-event-list"),
    path("shops/<uuid:shop_id>/stock-notification-preference/", StockNotificationPreferenceViewSet.as_view({"get": "retrieve", "patch": "partial_update"}), name="stock-notification-preference"),
    path("shops/<uuid:shop_id>/stock-alerts/", StockAlertViewSet.as_view({"get": "list"}), name="stock-alert-list"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/stock-counts/<uuid:pk>/adjustments/", StockCountViewSet.as_view({"get": "adjustments"}), name="stock-count-adjustments"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/stock-counts/<uuid:pk>/history/", StockCountViewSet.as_view({"get": "history"}), name="stock-count-history"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/stock-counts/<uuid:pk>/confirm/", StockCountViewSet.as_view({"post": "confirm"}), name="stock-count-confirm"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/stock-counts/", StockCountViewSet.as_view({"get": "list", "post": "create"}), name="stock-count-list"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/stock-counts/<uuid:pk>/", StockCountViewSet.as_view({"get": "retrieve"}), name="stock-count-detail"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/losses/", StockLossViewSet.as_view({"get": "list", "post": "create"}), name="stock-loss-list"),
    path("shops/<uuid:shop_id>/products/<uuid:product_id>/losses/<uuid:pk>/", StockLossViewSet.as_view({"get": "retrieve"}), name="stock-loss-detail"),
]
