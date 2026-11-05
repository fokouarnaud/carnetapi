"""Technical-only scheduler history; external trigger credentials are never shown."""
from django.contrib import admin
from backups.admin_base import TechnicalReadOnlyAdmin
from backups.models import BackupScheduleRun, BackupSchedulerState


@admin.register(BackupScheduleRun)
class BackupScheduleRunAdmin(TechnicalReadOnlyAdmin):
    list_display = ["sequence", "local_date", "scheduled_for", "missed_day_count", "job", "dispatch", "failure_code"]
    readonly_fields = [field.name for field in BackupScheduleRun._meta.fields]
    actions = None

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("actor", "job", "dispatch")


@admin.register(BackupSchedulerState)
class BackupSchedulerStateAdmin(TechnicalReadOnlyAdmin):
    list_display = ["id", "leased_until", "updated_at"]
    readonly_fields = ["id", "leased_until", "created_at", "updated_at"]
    fields = readonly_fields
    actions = None
