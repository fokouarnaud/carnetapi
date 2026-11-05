import uuid

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.db.models import OuterRef, Subquery
from django.shortcuts import get_object_or_404
from django.utils.html import format_html
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from rest_framework.exceptions import APIException, AuthenticationFailed, PermissionDenied as ApiPermissionDenied

from backups.models import BackupDispatch, BackupJob, SendAttempt
from backups.services.jobs import BackupPreparationBusy, BackupPreparationUnavailable, prepare_backup
from backups.services.abandonment import BackupCannotBeAbandoned, abandon_backup
from backups.services.dispatch import start_dispatch, advance_dispatch, dispatch_progress
from backups.services.health import backup_health
from backups.services.email_parts import BackupEmailError
from common.services.errors import OperationConflict
from identity.models import User


from backups.admin_base import technical, TechnicalReadOnlyAdmin


class PrepareBackupForm(forms.Form):
    operation_id = forms.UUIDField(widget=forms.HiddenInput)
    recipient = forms.EmailField(label=_("Recipient"), max_length=254)
    reason = forms.CharField(label=_("Reason"), max_length=2000, strip=True, widget=forms.Textarea)


class AbandonBackupForm(forms.Form):
    operation_id = forms.UUIDField(widget=forms.HiddenInput)
    reason = forms.CharField(label=_("Abandonment reason"), max_length=2000, strip=True, widget=forms.Textarea)


class StartDispatchForm(forms.Form):
    operation_id = forms.UUIDField(widget=forms.HiddenInput)
    reason = forms.CharField(label=_("Dispatch reason"), max_length=2000, strip=True, widget=forms.Textarea)


class AdvanceDispatchForm(forms.Form):
    action = forms.ChoiceField(choices=[("advance", "advance")], widget=forms.HiddenInput, initial="advance")


@admin.register(BackupJob)
class BackupJobAdmin(TechnicalReadOnlyAdmin):
    change_list_template = "admin/backups/change_list.html"
    list_display = ["id", "created_at", "requested_by", "state", "part_count", "archive_bytes", "cleanup_state", "cleaned_at", "abandoned_at", "dispatch_action", "abandon_action"]
    list_filter = ["state", "cleanup_state", "abandoned_at"]
    readonly_fields = [field.name for field in BackupJob._meta.fields if field.name != "preparation_slot"]
    exclude = ["preparation_slot"]

    def changelist_view(self, request, extra_context=None):
        if not technical(request):
            raise PermissionDenied()
        try:
            health = backup_health(request.user)
        except ApiPermissionDenied:
            raise PermissionDenied() from None
        return super().changelist_view(request, extra_context={**(extra_context or {}), "backup_health": health})

    def get_queryset(self, request):
        latest = BackupDispatch.objects.filter(job_id=OuterRef("pk")).order_by("-created_at", "-id")
        return super().get_queryset(request).annotate(
            active_dispatch_id=Subquery(latest.filter(state="active").values("id")[:1]),
            latest_dispatch_id=Subquery(latest.values("id")[:1]))

    @admin.display(description=_("Archive dispatch"))
    def dispatch_action(self, obj):
        command = obj.active_dispatch_id or obj.latest_dispatch_id
        if command:
            return format_html('<a href="{}">{}</a>', reverse("admin:backups_backupjob_dispatch_progress", args=[obj.id, command]), _("Dispatch progress"))
        if obj.state == "ready" and obj.abandoned_at is None and obj.cleanup_state == "waiting":
            return format_html('<a href="{}">{}</a>', reverse("admin:backups_backupjob_dispatch_start", args=[obj.id]), _("Send this archive"))
        return "—"

    def get_urls(self):
        return [path("prepare/", self.admin_site.admin_view(self.prepare_view), name="backups_backupjob_prepare"),
            path("<uuid:job_id>/abandon/", self.admin_site.admin_view(self.abandon_view), name="backups_backupjob_abandon"),
            path("<uuid:job_id>/dispatch/", self.admin_site.admin_view(self.start_dispatch_view), name="backups_backupjob_dispatch_start"),
            path("<uuid:job_id>/dispatch/<uuid:dispatch_id>/", self.admin_site.admin_view(self.dispatch_progress_view), name="backups_backupjob_dispatch_progress")] + super().get_urls()

    def start_dispatch_view(self, request, job_id):
        if not technical(request):
            raise PermissionDenied()
        job = get_object_or_404(BackupJob, id=job_id)
        form = StartDispatchForm(request.POST or None, initial={"operation_id": uuid.uuid4(), "reason": job.reason})
        if request.method == "POST" and form.is_valid():
            try:
                command, _created = start_dispatch(request.user, job.id, **form.cleaned_data)
            except (AuthenticationFailed, ApiPermissionDenied):
                raise PermissionDenied() from None
            except APIException:
                form.add_error(None, _("Unable to start this dispatch. Check its current status."))
            else:
                return HttpResponseRedirect(reverse("admin:backups_backupjob_dispatch_progress", args=[job.id, command.id]))
        return TemplateResponse(request, "admin/backups/dispatch_start.html", {**self.admin_site.each_context(request),
            "title": _("Send this archive"), "opts": self.model._meta, "form": form, "backup": job})

    def dispatch_progress_view(self, request, job_id, dispatch_id):
        if not technical(request):
            raise PermissionDenied()
        command = get_object_or_404(BackupDispatch.objects.select_related("job"), id=dispatch_id, job_id=job_id)
        form = AdvanceDispatchForm(request.POST if request.method == "POST" else None)
        if request.method == "POST" and form.is_valid():
            try:
                advance_dispatch(request.user, job_id, dispatch_id)
            except (AuthenticationFailed, ApiPermissionDenied):
                raise PermissionDenied() from None
            except (APIException, BackupEmailError):
                form.add_error(None, _("Unable to advance this dispatch. Check its current status and mail configuration."))
                command.refresh_from_db()
                command.job.refresh_from_db()
            else:
                self.message_user(request, _("Dispatch step checked. SMTP acceptance does not confirm delivery."), level=messages.INFO)
                return HttpResponseRedirect(reverse("admin:backups_backupjob_dispatch_progress", args=[job_id, dispatch_id]))
        progress = dispatch_progress(command)
        show_advance = command.state == "active" or (command.state == "accepted" and command.job.cleanup_state != "cleaned")
        return TemplateResponse(request, "admin/backups/dispatch_progress.html", {**self.admin_site.each_context(request),
            "title": _("Dispatch progress"), "opts": self.model._meta, "form": form, "command": command,
            "backup": command.job, **progress, "show_advance": show_advance,
            "can_restart": command.state == "incomplete" and command.job.abandoned_at is None,
            "attempts_url": reverse("admin:backups_sendattempt_changelist") + "?dispatch__id__exact=" + str(command.id)})

    @admin.display(description=_("Abandonment"))
    def abandon_action(self, obj):
        if obj.abandoned_at is None and obj.state in {"ready", "failed"} and obj.cleanup_state == "waiting":
            return format_html('<a href="{}">{}</a>', reverse("admin:backups_backupjob_abandon", args=[obj.id]), _("Abandon this backup"))
        return "—"

    def abandon_view(self, request, job_id):
        if not technical(request):
            raise PermissionDenied()
        job = get_object_or_404(BackupJob, id=job_id)
        form = AbandonBackupForm(request.POST or None, initial={"operation_id": uuid.uuid4()})
        if request.method == "POST" and form.is_valid():
            try:
                abandon_backup(request.user, job.id, **form.cleaned_data)
            except (AuthenticationFailed, ApiPermissionDenied):
                raise PermissionDenied() from None
            except (BackupCannotBeAbandoned, OperationConflict):
                form.add_error(None, _("Unable to abandon this backup. Check its current status."))
            else:
                self.message_user(request, _("Backup abandoned. Existing files and SMTP results are retained."), level=messages.SUCCESS)
                return HttpResponseRedirect(reverse("admin:backups_backupjob_changelist"))
        return TemplateResponse(request, "admin/backups/abandon.html", {**self.admin_site.each_context(request),
            "title": _("Abandon this backup"), "opts": self.model._meta, "form": form, "backup": job})

    def prepare_view(self, request):
        if not technical(request):
            raise PermissionDenied()
        form = PrepareBackupForm(request.POST or None, initial={"operation_id": uuid.uuid4()})
        if request.method == "POST" and form.is_valid():
            try:
                prepare_backup(request.user, form.cleaned_data)
            except (AuthenticationFailed, ApiPermissionDenied):
                raise PermissionDenied() from None
            except (BackupPreparationUnavailable, BackupPreparationBusy, OperationConflict):
                form.add_error(None, _("Unable to prepare backup. Check the backup status."))
            else:
                self.message_user(request, _("Backup prepared and verified. No email has been sent."), level=messages.SUCCESS)
                return HttpResponseRedirect(reverse("admin:backups_backupjob_changelist"))
        return TemplateResponse(request, "admin/backups/prepare.html", {**self.admin_site.each_context(request),
            "title": _("Prepare server backup"), "opts": self.model._meta, "form": form})


@admin.register(SendAttempt)
class SendAttemptAdmin(TechnicalReadOnlyAdmin):
    list_display = ["id", "created_at", "part", "number", "retry_number", "state", "possible_duplicate", "completed_at"]
    list_filter = ["state", "possible_duplicate"]
    readonly_fields = [field.name for field in SendAttempt._meta.fields]
    actions = None

    def get_queryset(self, request):
        rows = super().get_queryset(request).select_related("part", "part__job", "requested_by")
        return rows if technical(request) else rows.none()


@admin.register(BackupDispatch)
class BackupDispatchAdmin(TechnicalReadOnlyAdmin):
    list_display = ["id", "job", "requested_by", "state", "created_at", "finished_at", "progress_action"]
    list_filter = ["state"]
    exclude = ["lease_id", "leased_until"]
    readonly_fields = [field.name for field in BackupDispatch._meta.fields if field.name not in {"lease_id", "leased_until"}]
    actions = None

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("job", "requested_by")


    @admin.display(description=_("Dispatch progress"))
    def progress_action(self, obj):
        return format_html('<a href="{}">{}</a>', reverse("admin:backups_backupjob_dispatch_progress", args=[obj.job_id, obj.id]), _("Dispatch progress"))


# Register the versioned configuration views through Django autodiscovery.
from backups import configuration_admin  # noqa: E402,F401
from backups import scheduler_admin  # noqa: E402,F401
from backups import restore_admin  # noqa: E402,F401
from backups import quarantine_admin  # noqa: E402,F401
from backups import access_admin  # noqa: E402,F401
from backups import recovery_admin  # noqa: E402,F401
from backups import collection_review_admin  # noqa: E402,F401
from backups import hosting_admin  # noqa: E402,F401
