"""Audited schedule editing; generic admin CRUD cannot bypass the version check."""
import uuid
from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied as ApiPermissionDenied
from backups.admin_base import technical, TechnicalReadOnlyAdmin
from backups.models import BackupConfiguration, BackupConfigurationChange
from backups.services.configuration import current_configuration, configuration_snapshot, change_configuration
from backups.services.health import backup_health
from common.services.errors import OperationConflict


class BackupConfigurationForm(forms.Form):
    operation_id = forms.UUIDField(widget=forms.HiddenInput)
    expected_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    enabled = forms.BooleanField(label=_("Daily backups enabled"), required=False)
    local_hour = forms.IntegerField(label=_("Local hour"), min_value=0, max_value=23)
    local_minute = forms.IntegerField(label=_("Local minute"), min_value=0, max_value=59)
    reason = forms.CharField(label=_("Reason"), max_length=2000, strip=True, widget=forms.Textarea)


@admin.register(BackupConfiguration)
class BackupConfigurationAdmin(TechnicalReadOnlyAdmin):
    change_list_template = "admin/backups/configuration_change_list.html"
    list_display = ["id", "enabled", "local_hour", "local_minute", "timezone_name", "version", "updated_at"]
    readonly_fields = [field.name for field in BackupConfiguration._meta.fields if field.name != "singleton"]
    exclude = ["singleton"]
    actions = None

    def get_urls(self):
        return [path("configure/", self.admin_site.admin_view(self.configure_view),
            name="backups_backupconfiguration_configure")] + super().get_urls()

    def configure_view(self, request):
        if not technical(request):
            raise PermissionDenied()
        configuration = current_configuration()
        snapshot = configuration_snapshot(configuration)
        form = BackupConfigurationForm(request.POST if request.method == "POST" else None,
            initial={"operation_id": uuid.uuid4(), "expected_version": configuration.version,
                "enabled": configuration.enabled, "local_hour": configuration.local_hour, "local_minute": configuration.local_minute})
        if request.method == "POST" and form.is_valid():
            try:
                change_configuration(request.user, **form.cleaned_data)
            except (AuthenticationFailed, ApiPermissionDenied):
                raise PermissionDenied() from None
            except OperationConflict:
                form.add_error(None, _("Configuration changed. Reload this page before trying again."))
                snapshot = configuration_snapshot()
            else:
                self.message_user(request, _("Daily backup configuration saved. No backup has been executed."), level=messages.SUCCESS)
                return HttpResponseRedirect(reverse("admin:backups_backupconfiguration_configure"))
        try:
            health = backup_health(request.user)
        except ApiPermissionDenied:
            raise PermissionDenied() from None
        return TemplateResponse(request, "admin/backups/configuration.html", {**self.admin_site.each_context(request),
            "title": _("Daily backup configuration"), "opts": self.model._meta, "form": form, "schedule": snapshot,
            "backup_health": health})


@admin.register(BackupConfigurationChange)
class BackupConfigurationChangeAdmin(TechnicalReadOnlyAdmin):
    list_display = ["id", "created_at", "actor", "previous_version", "version", "reason"]
    readonly_fields = [field.name for field in BackupConfigurationChange._meta.fields if field.name != "payload"]
    exclude = ["payload"]
    actions = None

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("actor", "configuration")
