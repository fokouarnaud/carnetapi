from django.urls import path
from backups.api.views import BackupViewSet
from backups.api.configuration import BackupConfigurationView
from backups.api.trigger import BackupTriggerView
from backups.api.recovery_devices import RecoveryDeviceViewSet
from backups.api.recovery_journals import RecoveryJournalViewSet,RecoveryOperationViewSet
from backups.api.recovery_integration import RecoveryIntegrationViewSet
from backups.api.recovery_closures import RecoveryClosurePreviewView, RecoveryClosureView
from backups.api.recovery_balance import ShopRecoveryBalanceView, TechnicalRecoveryBalanceView
from backups.api.recovery_sale_holds import RecoverySaleHoldViewSet
from backups.api.recovery_collection_reviews import RecoveryCollectionReviewViewSet
from backups.api.recovery_reservation_reconciliation import RecoveryReservationReconciliationViewSet
from backups.api.hosting import HostingConfigurationView, HostingStatusView, HostingTriggerView, HostingReminderViewSet, HostingObservationViewSet


urlpatterns = [
    path("recovery/sessions/<uuid:session_id>/balance/",TechnicalRecoveryBalanceView.as_view(),name="technical-recovery-balance"),
    path("shops/<uuid:shop_id>/recovery/balance/",ShopRecoveryBalanceView.as_view(),name="shop-recovery-balance"),
    path("recovery/sessions/<uuid:session_id>/closure-preview/",RecoveryClosurePreviewView.as_view(),name="recovery-closure-preview"),
    path("recovery/sessions/<uuid:session_id>/closures/",RecoveryClosureView.as_view(),name="recovery-closure"),
    path("hosting/configuration/",HostingConfigurationView.as_view(),name="hosting-configuration"),
    path("hosting/status/",HostingStatusView.as_view(),name="hosting-status"),
    path("hosting/trigger/",HostingTriggerView.as_view(),name="hosting-trigger"),
    path("hosting/reminders/",HostingReminderViewSet.as_view({"get":"list"}),name="hosting-reminders"),
    path("hosting/reminders/<uuid:pk>/attempts/",HostingReminderViewSet.as_view({"get":"attempts"}),name="hosting-reminder-attempts"),
    path("hosting/reminders/<uuid:pk>/retry/",HostingReminderViewSet.as_view({"post":"retry"}),name="hosting-reminder-retry"),
    path("hosting/observations/",HostingObservationViewSet.as_view({"get":"list","post":"create"}),name="hosting-observations"),
    path("shops/<uuid:shop_id>/recovery/operations/<uuid:pk>/reservations/<uuid:reservation_id>/reconciliation-preview/",RecoveryReservationReconciliationViewSet.as_view({"get":"preview"}),name="recovery-reservation-reconciliation-preview"),
    path("shops/<uuid:shop_id>/recovery/operations/<uuid:pk>/reservations/<uuid:reservation_id>/reconciliations/",RecoveryReservationReconciliationViewSet.as_view({"get":"list","post":"create"}),name="recovery-reservation-reconciliation"),
    path("shops/<uuid:shop_id>/recovery/operations/<uuid:pk>/review/",RecoverySaleHoldViewSet.as_view({"post":"decide"}),name="recovery-sale-review"),
    path("shops/<uuid:shop_id>/recovery/operations/<uuid:pk>/review-decisions/",RecoverySaleHoldViewSet.as_view({"get":"history"}),name="recovery-sale-review-decisions"),
    path("shops/<uuid:shop_id>/recovery/sales-review/",RecoverySaleHoldViewSet.as_view({"get":"list"}),name="recovery-sale-review-list"),
    path("shops/<uuid:shop_id>/recovery/operations/<uuid:pk>/collection-review/",RecoveryCollectionReviewViewSet.as_view({"post":"decide"}),name="recovery-collection-review"),
    path("shops/<uuid:shop_id>/recovery/operations/<uuid:pk>/collection-review-decisions/",RecoveryCollectionReviewViewSet.as_view({"get":"history"}),name="recovery-collection-review-decisions"),
    path("shops/<uuid:shop_id>/recovery/collections-review/",RecoveryCollectionReviewViewSet.as_view({"get":"list"}),name="recovery-collection-review-list"),
    path("shops/<uuid:shop_id>/recovery/operations/<uuid:pk>/integration-preview/",RecoveryIntegrationViewSet.as_view({"get":"preview"}),name="recovery-integration-preview"),
    path("shops/<uuid:shop_id>/recovery/operations/<uuid:pk>/integrate/",RecoveryIntegrationViewSet.as_view({"post":"integrate"}),name="recovery-integration-apply"),
    path("shops/<uuid:shop_id>/recovery/journals/",RecoveryJournalViewSet.as_view({"get":"list","post":"create"}),name="recovery-journal-list"),
    path("shops/<uuid:shop_id>/recovery/journals/<uuid:pk>/",RecoveryJournalViewSet.as_view({"get":"retrieve"}),name="recovery-journal-detail"),
    path("shops/<uuid:shop_id>/recovery/operations/",RecoveryOperationViewSet.as_view({"get":"list"}),name="recovery-operation-list"),
    path("shops/<uuid:shop_id>/recovery/operations/<uuid:pk>/",RecoveryOperationViewSet.as_view({"get":"retrieve"}),name="recovery-operation-detail"),
    path("shops/<uuid:shop_id>/recovery/devices/",RecoveryDeviceViewSet.as_view({"get":"list"}),name="recovery-device-list"),
    path("shops/<uuid:shop_id>/recovery/devices/<uuid:pk>/",RecoveryDeviceViewSet.as_view({"get":"retrieve"}),name="recovery-device-detail"),
    path("backups/trigger/", BackupTriggerView.as_view(), name="backup-trigger"),
    path("backups/configuration/", BackupConfigurationView.as_view(), name="backup-configuration"),
    path("backups/", BackupViewSet.as_view({"get": "list", "post": "create"}), name="backup-list"),
    path("backups/<uuid:pk>/", BackupViewSet.as_view({"get": "retrieve"}), name="backup-detail"),
    path("backups/<uuid:pk>/parts/<int:number>/", BackupViewSet.as_view({"get": "download_part"}), name="backup-part"),
    path("backups/<uuid:pk>/parts/<int:number>/send/", BackupViewSet.as_view({"post": "send_part"}), name="backup-part-send"),
    path("backups/<uuid:pk>/parts/<int:number>/attempts/", BackupViewSet.as_view({"get": "attempts"}), name="backup-part-attempts"),
    path("backups/<uuid:pk>/abandon/", BackupViewSet.as_view({"post": "abandon"}), name="backup-abandon"),
    path("backups/<uuid:pk>/dispatches/",BackupViewSet.as_view({"post":"start_dispatch"}),name="backup-dispatch-start"),
    path("backups/<uuid:pk>/dispatches/<uuid:dispatch_id>/",BackupViewSet.as_view({"get":"retrieve_dispatch"}),name="backup-dispatch-detail"),
    path("backups/<uuid:pk>/dispatches/<uuid:dispatch_id>/advance/",BackupViewSet.as_view({"post":"advance_dispatch"}),name="backup-dispatch-advance"),
]
