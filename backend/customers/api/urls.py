"""Routes owned by the customers domain; external API paths remain stable."""
from customers.api.merges import CustomerMergeView
from django.urls import include, path
from customers.api.clients import ClientViewSet
from customers.api.return_credit_settlements import ClientReturnCreditViewSet, ReturnCreditSettlementViewSet
from customers.api.debts import CustomerDebtViewSet
from customers.api.payments import CustomerPaymentViewSet
from customers.api.regularizations import CreditRegularizationViewSet, PaymentRegularizationViewSet
from customers.api.statements import CustomerStatementViewSet
from customers.api.credits import CustomerCreditViewSet

urlpatterns = [
    path("shops/<uuid:shop_id>/sales/<uuid:sale_id>/returns/<uuid:return_id>/refunds/<uuid:pk>/", ReturnCreditSettlementViewSet.as_view({"get": "retrieve"}, kind="refund"), name="return-credit-refund"),
    path("shops/<uuid:shop_id>/sales/<uuid:sale_id>/returns/<uuid:return_id>/allocations/<uuid:pk>/", ReturnCreditSettlementViewSet.as_view({"get": "retrieve"}, kind="allocation"), name="return-credit-allocation"),
    path("shops/<uuid:shop_id>/clients/<uuid:client_id>/return-credits/", ClientReturnCreditViewSet.as_view({"get": "list"}), name="client-return-credits"),
    path("shops/<uuid:shop_id>/sales/<uuid:sale_id>/returns/<uuid:return_id>/refunds/", ReturnCreditSettlementViewSet.as_view({"get": "list", "post": "refund"}, kind="refund"), name="return-credit-refunds"),
    path("shops/<uuid:shop_id>/sales/<uuid:sale_id>/returns/<uuid:return_id>/allocations/", ReturnCreditSettlementViewSet.as_view({"get": "list", "post": "allocate"}, kind="allocation"), name="return-credit-allocations"),
    path("shops/<uuid:shop_id>/clients/<uuid:client_id>/credits/<uuid:payment_id>/allocate/", CreditRegularizationViewSet.as_view({"post": "allocate"}), name="customer-credit-allocate"),
    path("shops/<uuid:shop_id>/clients/<uuid:client_id>/credits/<uuid:payment_id>/refund/", CreditRegularizationViewSet.as_view({"post": "refund"}), name="customer-credit-refund"),
    path("shops/<uuid:shop_id>/clients/<uuid:client_id>/credits/<uuid:payment_id>/regularizations/", CreditRegularizationViewSet.as_view({"get": "list"}), name="customer-credit-regularizations"),
    path("shops/<uuid:shop_id>/clients/<uuid:client_id>/credits/", CustomerCreditViewSet.as_view({"get": "list"}), name="customer-credit-list"),
    path("shops/<uuid:shop_id>/clients/<uuid:client_id>/statement/", CustomerStatementViewSet.as_view({"get": "list"}), name="customer-statement"),
    path("shops/<uuid:shop_id>/clients/<uuid:client_id>/payments/<uuid:payment_id>/allocate/", PaymentRegularizationViewSet.as_view({"post": "allocate"}), name="customer-payment-allocate"),
    path("shops/<uuid:shop_id>/clients/<uuid:client_id>/payments/<uuid:payment_id>/refund/", PaymentRegularizationViewSet.as_view({"post": "refund"}), name="customer-payment-refund"),
    path("shops/<uuid:shop_id>/clients/<uuid:client_id>/payments/<uuid:payment_id>/regularizations/", PaymentRegularizationViewSet.as_view({"get": "list"}), name="customer-payment-regularizations"),
    path("shops/<uuid:shop_id>/clients/<uuid:client_id>/payments/", CustomerPaymentViewSet.as_view({"get": "list", "post": "create"}), name="customer-payment-list"),
    path("shops/<uuid:shop_id>/clients/<uuid:client_id>/payment-preview/", CustomerPaymentViewSet.as_view({"get": "preview"}), name="customer-payment-preview"),
    path("shops/<uuid:shop_id>/clients/<uuid:client_id>/debts/", CustomerDebtViewSet.as_view({"get": "list", "post": "create"}), name="customer-debt-list"),
    path("shops/<uuid:shop_id>/clients/<uuid:client_id>/debts/<uuid:pk>/due-date/", CustomerDebtViewSet.as_view({"post": "due_date"}), name="customer-debt-due-date"),
    path("shops/<uuid:shop_id>/clients/<uuid:client_id>/debts/<uuid:pk>/history/", CustomerDebtViewSet.as_view({"get": "history"}), name="customer-debt-history"),
    path("shops/<uuid:shop_id>/clients/<uuid:client_id>/merge/", CustomerMergeView.as_view(), name="client-merge"),
    path("shops/<uuid:shop_id>/clients/", ClientViewSet.as_view({"get": "list", "post": "create"}), name="client-list"),
    path("shops/<uuid:shop_id>/clients/<uuid:pk>/", ClientViewSet.as_view({"get": "retrieve", "patch": "partial_update"}), name="client-detail"),
    path("shops/<uuid:shop_id>/clients/<uuid:pk>/possible-duplicates/", ClientViewSet.as_view({"get": "possible_duplicates"}), name="client-possible-duplicates"),
    path("shops/<uuid:shop_id>/clients/<uuid:pk>/report-correction/", ClientViewSet.as_view({"post": "report_correction"}), name="client-report-correction"),
    path("shops/<uuid:shop_id>/clients/<uuid:pk>/history/", ClientViewSet.as_view({"get": "history"}), name="client-history"),
]
