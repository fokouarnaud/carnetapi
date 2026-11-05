"""Explicit access controls with historical receipts and no generic role editing."""
import uuid
from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.decorators import method_decorator
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods
from rest_framework.exceptions import PermissionDenied as ApiPermissionDenied, ValidationError
from backups.admin_base import TechnicalReadOnlyAdmin, technical
from backups.models import RestoreAccessDecision, RestoreAccessReview
from backups.services.access_decisions import decide_responsible_access,current_review_role
from common.services.errors import OperationConflict


STATES = {"pending":_("Pending review"),"confirmed":_("Confirmed"),"refused":_("Refused")}
ROLES = {"owner":_("Owner"),"manager":_("Manager"),"seller":_("Seller")}


class ArchivedRoleFilter(admin.SimpleListFilter):
    title=_("Archived role")
    parameter_name="role"
    def lookups(self,request,model_admin):
        return tuple(ROLES.items())
    def queryset(self,request,queryset):
        return queryset.filter(archived_role=self.value()) if self.value() in ROLES else queryset


class GenerationFilter(admin.RelatedOnlyFieldListFilter):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.title=_("Restoration access generation")


class AccessDecisionForm(forms.Form):
    operation_id = forms.UUIDField(widget=forms.HiddenInput)
    generation_id = forms.UUIDField(widget=forms.HiddenInput)
    previous_version = forms.IntegerField(min_value=0,widget=forms.HiddenInput)
    state = forms.ChoiceField(label=_("Decision"),choices=[("confirmed",STATES["confirmed"]),("refused",STATES["refused"])])
    reason = forms.CharField(label=_("Reason"),max_length=2000,widget=forms.Textarea)
    evidence_ref = forms.CharField(label=_("Evidence reference (optional)"),max_length=500,required=False)


@admin.register(RestoreAccessReview)
class RestoreAccessReviewAdmin(TechnicalReadOnlyAdmin):
    list_display = ["id","generation_display","shop","user","archived_role_label","current_state_label","version_display","control_link"]
    list_display_links = None
    list_filter = [("generation",GenerationFilter),ArchivedRoleFilter]
    readonly_fields = [field.name for field in RestoreAccessReview._meta.fields]
    list_per_page = 25
    actions = None

    def get_queryset(self,request):
        latest=RestoreAccessDecision.objects.filter(review_id=OuterRef("pk")).order_by("-version")
        return super().get_queryset(request).select_related("shop","user","generation","membership").annotate(
            current_state=Coalesce(Subquery(latest.values("state")[:1]),Value("pending")),
            current_version=Coalesce(Subquery(latest.values("version")[:1]),Value(0)))

    @admin.display(description=_("Current review status"))
    def current_state_label(self,obj):
        return STATES[obj.current_state]

    @admin.display(description=_("Archived role"),ordering="archived_role")
    def archived_role_label(self,obj):
        return ROLES[obj.archived_role]

    @admin.display(description=_("Restoration access generation"),ordering="generation__created_at")
    def generation_display(self,obj):
        label=_("Current generation") if obj.generation.current_slot==1 else _("Historical generation")
        return f"{obj.generation_id} · {label}"

    @admin.display(description=_("Version"),ordering="current_version")
    def version_display(self,obj):
        return obj.current_version

    @admin.display(description=_("Review restored access"))
    def control_link(self,obj):
        if obj.generation.current_slot != 1 or current_review_role(obj) not in ("owner","manager"):
            return "—"
        return format_html('<a href="{}">{}</a>',reverse("admin:backups_restoreaccessreview_control",args=[obj.id]),_("Review restored access"))

    def get_urls(self):
        return [path("<uuid:review_id>/control/",self.admin_site.admin_view(self.control_view),
            name="backups_restoreaccessreview_control")]+super().get_urls()

    @method_decorator(require_http_methods(["GET","POST"]))
    def control_view(self,request,review_id):
        if not technical(request):
            raise PermissionDenied()
        review=get_object_or_404(self.get_queryset(request),id=review_id,generation__current_slot=1)
        if current_review_role(review) not in ("owner","manager"):
            raise PermissionDenied()
        form=AccessDecisionForm(request.POST if request.method=="POST" else None,initial={
            "operation_id":uuid.uuid4(),"generation_id":review.generation_id,"previous_version":review.current_version})
        status=200
        if request.method=="POST":
            if request.FILES or getattr(request,"restore_upload_rejected",False):
                form.is_valid()
                form.add_error(None,_("Unable to record this access decision."))
            elif form.is_valid():
                try:
                    data=dict(form.cleaned_data)
                    if data.pop("generation_id") != review.generation_id:
                        raise OperationConflict()
                    decision,_created=decide_responsible_access(actor=request.user,review_id=review.id,**data)
                except ApiPermissionDenied:
                    raise PermissionDenied() from None
                except OperationConflict:
                    status=409
                    form.add_error(None,_("The review changed. Reload before recording another decision."))
                except ValidationError:
                    form.add_error(None,_("Unable to record this access decision."))
                else:
                    return HttpResponseRedirect(reverse("admin:backups_restoreaccessreview_control",args=[review.id])+"?decision="+str(decision.id))
        receipt=None
        if request.GET.get("decision"):
            try:
                receipt=get_object_or_404(RestoreAccessDecision,review=review,id=uuid.UUID(request.GET["decision"]))
            except ValueError:
                raise Http404() from None
        history=Paginator(review.decisions.select_related("requested_by").order_by("-version"),25).get_page(request.GET.get("history_page",1))
        return TemplateResponse(request,"admin/backups/access_control.html",{**self.admin_site.each_context(request),
            "title":_("Review restored access"),"opts":self.model._meta,"review":review,"form":form,
            "archived_role_label":ROLES[review.archived_role],
            "current_role_label":ROLES[current_review_role(review)],
            "current_state_label":STATES[review.current_state],"receipt":receipt,
            "receipt_state_label":STATES[receipt.state] if receipt else None,"history":history,
            "history_rows":[{"event":event,"state_label":STATES[event.state]} for event in history]},status=status)
