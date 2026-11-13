"""Routes owned by the purchasing domain; external API paths remain stable."""
from purchasing.api.line_closures import PurchaseLineClosureView
from django.urls import include, path
from purchasing.api.supplier_statements import SupplierStatementViewSet
from purchasing.api.suppliers import SupplierViewSet
from purchasing.api.purchases import PurchaseReceiptViewSet, PurchaseViewSet, SupplierDebtViewSet
from purchasing.api.purchase_charges import PurchaseChargeRevisionViewSet, PurchaseChargeViewSet
from purchasing.api.supplier_payments import SupplierPaymentViewSet
from purchasing.api.supplier_returns import SupplierReturnViewSet
from purchasing.api.supplier_credits import SupplierCreditViewSet

urlpatterns = [
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/invoices/<uuid:document_id>/lines/<uuid:line_id>/close/", PurchaseLineClosureView.as_view(), name="purchase-line-close"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/invoices/<uuid:document_id>/charges/preview/", PurchaseChargeViewSet.as_view({"post": "preview"}), name="purchase-charge-preview"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/invoices/<uuid:document_id>/charges/", PurchaseChargeViewSet.as_view({"get": "list", "post": "create"}), name="purchase-charges"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/invoices/<uuid:document_id>/charges/<uuid:pk>/", PurchaseChargeViewSet.as_view({"get": "retrieve"}), name="purchase-charge"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/invoices/<uuid:document_id>/charges/<uuid:charge_id>/revisions/", PurchaseChargeRevisionViewSet.as_view({"get": "list", "post": "create"}), name="purchase-charge-revisions"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/statement/", SupplierStatementViewSet.as_view({"get": "list"}), name="supplier-statement"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/opening-balances/", SupplierCreditViewSet.as_view({"get": "list_openings", "post": "create_opening"}), name="supplier-opening-balances"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/credits/<uuid:pk>/history/", SupplierCreditViewSet.as_view({"get": "history"}), name="supplier-credit-history"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/credits/<uuid:pk>/allocate/", SupplierCreditViewSet.as_view({"post": "allocate"}), name="supplier-credit-allocate"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/credits/", SupplierCreditViewSet.as_view({"get": "list", "post": "create"}), name="supplier-credit-list"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/credits/<uuid:pk>/", SupplierCreditViewSet.as_view({"get": "retrieve"}), name="supplier-credit-detail"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/credits/<uuid:pk>/refund/", SupplierCreditViewSet.as_view({"post": "refund"}), name="supplier-credit-refund"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/invoices/<uuid:document_id>/returns/", SupplierReturnViewSet.as_view({"get": "list", "post": "create"}), name="supplier-return-list"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/payments/", SupplierPaymentViewSet.as_view({"get": "list", "post": "create"}), name="supplier-payment-list"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/payments/<uuid:pk>/", SupplierPaymentViewSet.as_view({"get": "retrieve"}), name="supplier-payment-detail"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/payments/<uuid:pk>/allocate/", SupplierPaymentViewSet.as_view({"post": "allocate"}), name="supplier-payment-allocate"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/invoices/<uuid:document_id>/receipts/", PurchaseReceiptViewSet.as_view({"get": "list", "post": "create"}), name="purchase-receipt-list"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/orders/", PurchaseViewSet.as_view({"get": "list", "post": "create"}, kind="order"), name="purchase-order-list"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/orders/<uuid:pk>/", PurchaseViewSet.as_view({"get": "retrieve"}, kind="order"), name="purchase-order-detail"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/invoices/", PurchaseViewSet.as_view({"get": "list", "post": "create"}), name="purchase-invoice-list"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/invoices/<uuid:pk>/", PurchaseViewSet.as_view({"get": "retrieve"}), name="purchase-invoice-detail"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:supplier_id>/debts/", SupplierDebtViewSet.as_view({"get": "list", "post": "create"}), name="supplier-debt-list"),
    path("shops/<uuid:shop_id>/suppliers/", SupplierViewSet.as_view({"get": "list", "post": "create"}), name="supplier-list"),
    path("shops/<uuid:shop_id>/suppliers/<uuid:pk>/", SupplierViewSet.as_view({"get": "retrieve"}), name="supplier-detail"),
]
