"""Technical recovery inventory inspection, without identity or completion edits."""
import uuid
from django.contrib import admin
from django.db.models import Count
from django.utils.translation import gettext_lazy as _
from django.urls import path, reverse
from django.utils.html import format_html
from backups.admin_base import TechnicalReadOnlyAdmin
from backups.access_admin import GenerationFilter
from backups.models import RestoreSession,RecoveryDevice
from backups.closure_admin import partial_closure_view


class RecoveryReadOnlyAdmin(TechnicalReadOnlyAdmin):
    change_list_template="admin/backups/recovery_list.html"
    list_display_links=None
    list_per_page=25
    list_max_show_all=0
    show_full_result_count=False
    actions=None

    def get_queryset(self,request):
        queryset=super().get_queryset(request)
        identity=getattr(request,"restore_recovery_user_id",None)
        generation=getattr(request,"restore_recovery_generation_id",None)
        if identity is None and generation is None:
            return queryset
        if identity is None or generation is None:
            return queryset.none()
        try:
            generation=uuid.UUID(str(generation))
        except (ValueError,TypeError,AttributeError):
            return queryset.none()
        return queryset.filter(**{self.generation_lookup:generation,
            self.generation_lookup.removesuffix("_id")+"__current_slot":1})

    @admin.display(description=_("Restoration access generation"))
    def generation_display(self,obj):
        generation=self.generation_for(obj)
        state=_("Current generation") if generation.current_slot==1 else _("Historical generation")
        return f"{generation.id} · {state}"


@admin.register(RestoreSession)
class RestoreSessionAdmin(RecoveryReadOnlyAdmin):
    generation_lookup="generation_id"
    list_display=["id","generation_display","snapshot_display","inventory_display","closure_link","installations_display","observations_display"]
    readonly_fields=list_display
    fields=readonly_fields
    list_filter=[("generation",GenerationFilter)]

    def generation_for(self,obj):
        return obj.generation

    close_view=partial_closure_view

    def get_urls(self):
        return [path("<uuid:session_id>/close/",self.admin_site.admin_view(self.close_view),
            name="backups_restoresession_close")]+super().get_urls()

    @admin.display(description=_("Partial recovery closure"))
    def closure_link(self,obj):
        if obj.generation.current_slot!=1:return "—"
        return format_html('<a href="{}">{}</a>',reverse("admin:backups_restoresession_close",args=[obj.id]),_("Close recovery partially"))

    def get_queryset(self,request):
        return super().get_queryset(request).select_related("generation").annotate(
            installation_total=Count("devices__origin_installation_id",distinct=True),observation_total=Count("devices"))

    @admin.display(description=_("Backup snapshot (UTC)"),ordering="snapshot_at")
    def snapshot_display(self,obj):
        return obj.snapshot_at if obj.snapshot_at is not None else _("Unknown")

    @admin.display(description=_("Inventory recorded at (UTC)"),ordering="inventory_recorded_at")
    def inventory_display(self,obj):
        return obj.inventory_recorded_at if obj.inventory_recorded_at is not None else _("Inventory not recorded")

    @admin.display(description=_("Known installations"),ordering="installation_total")
    def installations_display(self,obj):
        return obj.installation_total if obj.inventory_recorded_at is not None else _("Inventory not recorded")

    @admin.display(description=_("Archived observations"),ordering="observation_total")
    def observations_display(self,obj):
        return obj.observation_total if obj.inventory_recorded_at is not None else _("Inventory not recorded")


@admin.register(RecoveryDevice)
class RecoveryDeviceAdmin(RecoveryReadOnlyAdmin):
    generation_lookup="session__generation_id"
    list_display=["id","generation_display","shop_display","installation_display","user_display","label_display","recorded_display"]
    readonly_fields=list_display
    fields=readonly_fields
    list_filter=[("session__generation",GenerationFilter)]

    def generation_for(self,obj):
        return obj.session.generation

    def get_queryset(self,request):
        return super().get_queryset(request).select_related("session__generation","shop")

    @admin.display(description=_("Shop"),ordering="shop__name")
    def shop_display(self,obj):
        return obj.shop.name

    @admin.display(description=_("Original installation UUID"),ordering="origin_installation_id")
    def installation_display(self,obj):
        return obj.origin_installation_id

    @admin.display(description=_("Original account UUID"),ordering="origin_user_id")
    def user_display(self,obj):
        return obj.origin_user_id

    @admin.display(description=_("Device label"),ordering="label")
    def label_display(self,obj):
        return obj.label

    @admin.display(description=_("Observation recorded at (UTC)"),ordering="created_at")
    def recorded_display(self,obj):
        return obj.created_at
