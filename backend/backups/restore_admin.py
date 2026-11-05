"""Technical, read-only import metadata plus explicit encrypted-part collection."""
import uuid
from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.decorators import method_decorator
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods
from rest_framework.exceptions import PermissionDenied as ApiPermissionDenied
from backups.admin_base import technical, TechnicalReadOnlyAdmin
from backups.models import ImportPreparation, RestoreImport, RestoreImportPart
from backups.services.archive import MAX_PART_BYTES
from backups.services.restore_imports import collect_part, import_progress, RestoreImportError
from backups.services.import_preparation import inspect_preparation, prepare_import, preparation_directory
from backups.services.jobs import authorize
from common.services.errors import OperationConflict


class ImportPartForm(forms.Form):
    batch_id = forms.UUIDField(widget=forms.HiddenInput)
    reason = forms.CharField(label=_("Reason"), max_length=2000, widget=forms.Textarea)
    encryption_key = forms.CharField(label=_("Archive encryption key"), max_length=256,
        widget=forms.PasswordInput(render_value=False), help_text=_("The key is used for this request only and is not stored."))
    part = forms.FileField(label=_("Encrypted backup part"), help_text=_("Upload one encrypted part of at most 15 MB."))

    def clean_part(self):
        part = self.cleaned_data["part"]
        if not 0 < part.size <= MAX_PART_BYTES:
            raise forms.ValidationError(_("Upload one encrypted part of at most 15 MB."))
        return part


class PrepareImportedArchiveForm(forms.Form):
    operation_id = forms.UUIDField(widget=forms.HiddenInput)
    reason = forms.CharField(label=_("Reason"), max_length=2000, widget=forms.Textarea)
    encryption_key = forms.CharField(label=_("Archive encryption key"), max_length=256,
        widget=forms.PasswordInput(render_value=False), help_text=_("The key is used for this request only and is not stored."))


@admin.register(RestoreImport)
class RestoreImportAdmin(TechnicalReadOnlyAdmin):
    change_list_template = "admin/backups/restore_change_list.html"
    list_display = ["id", "backup_id", "created_at", "requested_by", "part_count", "collected_count", "inventory_link"]
    readonly_fields = [field.name for field in RestoreImport._meta.fields]
    actions = None

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("requested_by").annotate(received_parts=Count("parts"))

    @admin.display(description=_("Recorded parts"))
    def collected_count(self, obj):
        return obj.received_parts

    @admin.display(description=_("Import inventory"))
    def inventory_link(self, obj):
        return format_html('<a href="{}">{}</a>', reverse("admin:backups_restoreimport_inventory", args=[obj.id]), _("Import inventory"))

    def get_urls(self):
        return [
            path("receive/", self.admin_site.admin_view(self.receive_view), name="backups_restoreimport_start"),
            path("<uuid:batch_id>/receive/", self.admin_site.admin_view(self.receive_view), name="backups_restoreimport_receive"),
            path("<uuid:batch_id>/inventory/", self.admin_site.admin_view(self.inventory_view), name="backups_restoreimport_inventory"),
            path("<uuid:batch_id>/prepare/", self.admin_site.admin_view(self.prepare_view), name="backups_restoreimport_prepare"),
        ] + super().get_urls()

    @method_decorator(sensitive_variables())
    @method_decorator(sensitive_post_parameters("encryption_key"))
    def prepare_view(self, request, batch_id):
        if not technical(request):
            raise PermissionDenied()
        batch = get_object_or_404(RestoreImport, id=batch_id)
        form = PrepareImportedArchiveForm(request.POST if request.method == "POST" else None,
            initial={"operation_id": uuid.uuid4(), "reason": batch.reason})
        if getattr(request, "restore_upload_rejected", False):
            form.is_valid()
            form.add_error(None, _("This preparation request does not accept uploaded files."))
        elif request.method == "POST" and form.is_valid():
            try:
                record, created = prepare_import(request.user, batch.id, form.cleaned_data["operation_id"],
                    form.cleaned_data["reason"], form.cleaned_data["encryption_key"])
            except ApiPermissionDenied:
                raise PermissionDenied() from None
            except (RestoreImportError, OperationConflict):
                form.add_error(None, _("Unable to start preparation. Check all parts and current preparations."))
            else:
                return HttpResponseRedirect(reverse("admin:backups_importpreparation_result", args=[record.id]))
        return TemplateResponse(request, "admin/backups/import_prepare.html", {**self.admin_site.each_context(request),
            "title": _("Prepare a restore copy"), "opts": self.model._meta, "batch": batch, "form": form})

    @method_decorator(sensitive_variables())
    @method_decorator(sensitive_post_parameters("encryption_key"))
    def receive_view(self, request, batch_id=None):
        if not technical(request):
            raise PermissionDenied()
        batch = get_object_or_404(RestoreImport, id=batch_id) if batch_id else None
        form = ImportPartForm(request.POST if request.method == "POST" else None, request.FILES or None,
            initial={"batch_id": batch_id or uuid.uuid4(), "reason": batch.reason if batch else ""})
        if getattr(request, "restore_upload_rejected", False):
            form.is_valid()
            form.add_error(None, _("Upload one encrypted part of at most 15 MB."))
        elif request.method == "POST" and form.is_valid():
            if batch_id is not None and form.cleaned_data["batch_id"] != batch_id:
                raise PermissionDenied()
            try:
                collected, collected_part, created = collect_part(request.user, form.cleaned_data["batch_id"], form.cleaned_data["reason"],
                    form.cleaned_data["part"], form.cleaned_data["encryption_key"])
            except ApiPermissionDenied:
                raise PermissionDenied() from None
            except (RestoreImportError, OperationConflict):
                form.add_error(None, _("Unable to collect this part. Check the key, archive and current import."))
            else:
                self.message_user(request, _("Encrypted part collected. Full archive verification is still required."), messages.SUCCESS)
                return HttpResponseRedirect(reverse("admin:backups_restoreimport_inventory", args=[collected.id]))
        return TemplateResponse(request, "admin/backups/restore_receive.html", {**self.admin_site.each_context(request),
            "title": _("Import backup parts"), "opts": self.model._meta, "form": form})

    def inventory_view(self, request, batch_id):
        if not technical(request):
            raise PermissionDenied()
        try:
            progress = import_progress(request.user, batch_id, page=int(request.GET.get("page", "1")))
        except ApiPermissionDenied:
            raise PermissionDenied() from None
        except (ValueError, RestoreImportError):
            return HttpResponseBadRequest(_("Invalid page."))
        return TemplateResponse(request, "admin/backups/restore_inventory.html", {**self.admin_site.each_context(request),
            "title": _("Import inventory"), "opts": self.model._meta, **progress,
            "preparations": progress["batch"].preparations.order_by("-created_at", "-id")[:10]})


@admin.register(RestoreImportPart)
class RestoreImportPartAdmin(TechnicalReadOnlyAdmin):
    list_display = ["batch", "number", "size_bytes", "uploaded_by", "created_at"]
    readonly_fields = [field.name for field in RestoreImportPart._meta.fields]
    actions = None

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("batch", "uploaded_by")


@admin.register(ImportPreparation)
class ImportPreparationAdmin(TechnicalReadOnlyAdmin):
    list_display = ["id", "batch", "requested_by", "created_at", "state", "finished_at", "result_link"]
    readonly_fields = [field.name for field in ImportPreparation._meta.fields if field.name != "worker_slot"]
    exclude = ["worker_slot"]
    list_filter = ["state"]
    actions = None

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("batch", "requested_by")

    @admin.display(description=_("Preparation result"))
    def result_link(self, obj):
        return format_html('<a href="{}">{}</a>', reverse("admin:backups_importpreparation_result", args=[obj.id]), _("Preparation result"))

    def get_urls(self):
        return [path("<uuid:preparation_id>/result/", self.admin_site.admin_view(self.result_view),
            name="backups_importpreparation_result")] + super().get_urls()

    @method_decorator(require_http_methods(["GET", "POST"]))
    def result_view(self, request, preparation_id):
        if not technical(request):
            raise PermissionDenied()
        record = get_object_or_404(ImportPreparation.objects.select_related("batch", "requested_by"), id=preparation_id)
        inspection = None
        inspection_failed = False
        try:
            authorize(request.user)
            if request.method == "POST":
                # Parse before examining the upload handler's rejection flag.
                request.POST
                if request.FILES or getattr(request, "restore_upload_rejected", False):
                    raise RestoreImportError("restore_preparation_receipt_invalid")
                inspection = inspect_preparation(request.user, record.id)
            path = preparation_directory(record) / "candidate.sqlite3"
            copy_available = not path.is_symlink() and path.is_file() and path.stat().st_size == record.candidate_bytes
        except ApiPermissionDenied:
            raise PermissionDenied() from None
        except (RestoreImportError, OSError):
            copy_available = False
            inspection_failed = request.method == "POST"
        return TemplateResponse(request, "admin/backups/import_preparation_result.html", {**self.admin_site.each_context(request),
            "title": _("Preparation result"), "opts": self.model._meta,
            "preparation": record, "copy_available": copy_available, "activation_allowed": False,
            "inspection": inspection, "inspection_failed": inspection_failed,
            "quarantines": record.quarantines.order_by("-created_at", "-id")[:10]})
