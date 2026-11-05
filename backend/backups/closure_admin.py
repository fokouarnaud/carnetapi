"""Two-step technical partial closure with a signed, concrete decision preview."""
import uuid

from django import forms
from django.contrib import admin
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods
from rest_framework.exceptions import PermissionDenied as ApiPermissionDenied, ValidationError

from backups.admin_base import TechnicalReadOnlyAdmin, technical
from backups.models import RecoveryClosure, RecoveryDevice
from backups.services.recovery_closures import preview_recovery_closure, close_recovery
from common.services.errors import OperationConflict

_SALT="recovery-partial-closure-confirmation"


class PartialClosureForm(forms.Form):
    operation_id=forms.UUIDField(widget=forms.HiddenInput)
    reason=forms.CharField(label=_("Reason"),max_length=2000,widget=forms.Textarea)
    missing_device_ids=forms.ModelMultipleChoiceField(label=_("Permanently unavailable device observations"),
        queryset=RecoveryDevice.objects.none(),widget=forms.CheckboxSelectMultiple)

    def __init__(self,*args,registry,**kwargs):
        super().__init__(*args,**kwargs)
        field=self.fields["missing_device_ids"]
        field.queryset=registry.devices.select_related("shop").order_by("shop__name","id")
        field.label_from_instance=lambda device:f"{device.shop.name} · {device.label} · {device.origin_installation_id} · {device.id}"


class ClosureConfirmationForm(forms.Form):
    signed_request=forms.CharField(widget=forms.HiddenInput,max_length=200000)
    confirmed=forms.BooleanField(label=_("I confirm these devices are permanently unavailable and the recovery remains partial."))


@method_decorator(require_http_methods(["GET","POST"]))
def partial_closure_view(model_admin,request,session_id):
    if not technical(request):raise PermissionDenied()
    registry=get_object_or_404(model_admin.get_queryset(request),id=session_id,generation__current_slot=1)
    form=PartialClosureForm(request.POST if request.method=="POST" and request.POST.get("stage")=="preview" else None,
        registry=registry,initial={"operation_id":uuid.uuid4()})
    confirmation=None;preview=None;selected=[];reason="";status=200
    if request.method=="POST":
        if request.FILES or getattr(request,"restore_upload_rejected",False):
            form.is_valid();form.add_error(None,_("Unable to record this partial closure."));status=400
        elif request.POST.get("stage")=="preview" and form.is_valid():
            preview=preview_recovery_closure(actor=request.user,restore_session_id=registry.id)
            selected=list(form.cleaned_data["missing_device_ids"])
            reason=form.cleaned_data["reason"]
            command={"operation_id":str(form.cleaned_data["operation_id"]),"restore_session_id":str(registry.id),
                "kind":"partial","reason":reason,"missing_device_ids":[str(device.id) for device in selected],
                "preview_token":preview["preview_token"]}
            confirmation=ClosureConfirmationForm(initial={"signed_request":signing.dumps(
                {"actor_id":str(request.user.id),"command":command},salt=_SALT,compress=True)})
        elif request.POST.get("stage")=="confirm":
            confirmation=ClosureConfirmationForm(request.POST)
            if confirmation.is_valid():
                try:
                    signed=signing.loads(confirmation.cleaned_data["signed_request"],salt=_SALT)
                    if not isinstance(signed,dict) or set(signed)!={"actor_id","command"}:raise signing.BadSignature()
                    if signed["actor_id"]!=str(request.user.id):raise PermissionDenied()
                    if not isinstance(signed["command"],dict) or signed["command"].get("restore_session_id")!=str(registry.id):
                        raise signing.BadSignature()
                    receipt,_created=close_recovery(actor=request.user,data=signed["command"])
                except signing.BadSignature:
                    status=400;confirmation.add_error(None,_("Invalid closure confirmation. Prepare a new preview."))
                except ApiPermissionDenied:
                    raise PermissionDenied() from None
                except OperationConflict:
                    status=409;confirmation.add_error(None,_("Recovery changed. Prepare a new preview before closing."))
                except ValidationError:
                    status=400;confirmation.add_error(None,_("Unable to record this partial closure."))
                else:
                    return HttpResponseRedirect(reverse("admin:backups_restoresession_close",args=[registry.id])+"?closure="+receipt["id"])
        elif request.POST.get("stage")!="preview":
            status=400;form.is_valid();form.add_error(None,_("Unable to record this partial closure."))
    closure=RecoveryClosure.objects.filter(session=registry).first()
    return TemplateResponse(request,"admin/backups/recovery_close.html",{**model_admin.admin_site.each_context(request),
        "title":_("Close recovery partially"),"opts":model_admin.model._meta,"registry":registry,"form":form,
        "confirmation_form":confirmation,"preview":preview,"selected":selected,"reason":reason,
        "receipt":closure.receipt if closure else None},status=status)


@admin.register(RecoveryClosure)
class RecoveryClosureAdmin(TechnicalReadOnlyAdmin):
    list_display=["id","session","closed_by","created_at","reason"]
    readonly_fields=[field.name for field in RecoveryClosure._meta.fields]
    list_per_page=25
    actions=None
