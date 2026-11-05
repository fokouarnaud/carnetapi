"""Explicit technical quarantine commands; no generic edit or activation."""
import uuid
from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.decorators import method_decorator
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods
from rest_framework.exceptions import PermissionDenied as ApiPermissionDenied
from backups.admin_base import TechnicalReadOnlyAdmin, technical
from backups.models import ImportPreparation, ImportQuarantine
from backups.services.import_quarantine import quarantine_directory, quarantine_import
from backups.services.jobs import authorize
from backups.services.restore_imports import RestoreImportError
from common.services.errors import OperationConflict


class QuarantineForm(forms.Form):
    operation_id = forms.UUIDField(widget=forms.HiddenInput)
    reason = forms.CharField(label=_("Reason"), max_length=2000, widget=forms.Textarea)


@admin.register(ImportQuarantine)
class ImportQuarantineAdmin(TechnicalReadOnlyAdmin):
    list_display = ["id", "preparation", "requested_by", "created_at", "state", "finished_at", "result_link"]
    readonly_fields = [field.name for field in ImportQuarantine._meta.fields if field.name != "worker_slot"]
    exclude = ["worker_slot"]
    list_filter = ["state"]
    actions = None

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("preparation", "requested_by")

    @admin.display(description=_("Quarantine result"))
    def result_link(self, obj):
        return format_html('<a href="{}">{}</a>', reverse("admin:backups_importquarantine_result", args=[obj.id]), _("Quarantine result"))

    def get_urls(self):
        return [path("<uuid:preparation_id>/start/", self.admin_site.admin_view(self.start_view), name="backups_importquarantine_start"),
            path("<uuid:quarantine_id>/result/", self.admin_site.admin_view(self.result_view), name="backups_importquarantine_result")] + super().get_urls()

    @method_decorator(require_http_methods(["GET", "POST"]))
    def start_view(self, request, preparation_id):
        if not technical(request):
            raise PermissionDenied()
        source = get_object_or_404(ImportPreparation.objects.select_related("batch"), id=preparation_id)
        form = QuarantineForm(request.POST if request.method == "POST" else None, initial={"operation_id":uuid.uuid4()})
        if request.method == "POST":
            if request.FILES or getattr(request, "restore_upload_rejected", False):
                form.is_valid()
                form.add_error(None, _("Unable to quarantine this copy."))
            elif form.is_valid():
                try:
                    record, _created = quarantine_import(request.user, source.id, **form.cleaned_data)
                except ApiPermissionDenied:
                    raise PermissionDenied() from None
                except (RestoreImportError, OperationConflict):
                    form.add_error(None, _("Unable to quarantine this copy."))
                else:
                    return HttpResponseRedirect(reverse("admin:backups_importquarantine_result", args=[record.id]))
        return TemplateResponse(request, "admin/backups/import_quarantine_start.html", {**self.admin_site.each_context(request),
            "title":_("Quarantine the copy"),"opts":self.model._meta,"preparation":source,"form":form})

    @method_decorator(require_http_methods(["GET"]))
    def result_view(self, request, quarantine_id):
        if not technical(request):
            raise PermissionDenied()
        record = get_object_or_404(ImportQuarantine.objects.select_related("preparation__batch", "requested_by"), id=quarantine_id)
        try:
            authorize(request.user)
            candidate = quarantine_directory(record)/"candidate.sqlite3"
            copy_available = not candidate.is_symlink() and candidate.is_file() and candidate.stat().st_size == record.candidate_bytes
        except ApiPermissionDenied:
            raise PermissionDenied() from None
        except (RestoreImportError, OSError):
            copy_available = False
        labels = (("active_users",_("Active accounts")),("active_memberships",_("Active team memberships")),
            ("privileged_users",_("Administrative privileges")),("group_assignments",_("Group assignments")),
            ("direct_permissions",_("Direct permissions")),("unrevoked_installations",_("Installations without revocation")),
            ("unrevoked_sessions",_("Sessions without revocation")),("verified_shop_leases",_("Shop access leases")),("web_sessions",_("Web sessions")))
        counts = [(label,record.manifest["before"][name],record.manifest["after"][name]) for name,label in labels] if record.state == "quarantined" else []
        return TemplateResponse(request, "admin/backups/import_quarantine_result.html", {**self.admin_site.each_context(request),
            "title":_("Quarantine result"),"opts":self.model._meta,"quarantine":record,"counts":counts,"copy_available":copy_available})
