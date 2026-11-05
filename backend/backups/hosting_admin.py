"""Technical Unfold forms route every hosting mutation through audited services."""
import uuid
from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from rest_framework.exceptions import APIException
from backups.admin_base import technical, TechnicalReadOnlyAdmin
from backups.models import HostingConfiguration, HostingConfigurationChange, HostingReminder, HostingReminderAttempt, HostingObservation, HostingReminderRepair
from backups.services.hosting import configuration_data, change_hosting, hosting_status, retry_hosting, record_hosting_observation


STATE_LABELS={"pending":_("Pending"),"sending":_("Sending"),
    "accepted":_("Accepted by SMTP, delivery unconfirmed"),"failed":_("Failed"),
    "uncertain":_("Acceptance uncertain"),"missed":_("Reminder window missed"),
    "unknown":_("Unknown"),"expired":_("Expired"),"expiring":_("Expiration approaching"),"active":_("Before reported expiration")}


def operator_status(request):
    try:return hosting_status(request.user)
    except APIException as error:
        if error.status_code in (401,403):raise PermissionDenied() from None
        raise


class HostingForm(forms.Form):
    operation_id=forms.UUIDField(widget=forms.HiddenInput)
    expected_version=forms.IntegerField(min_value=0,widget=forms.HiddenInput)
    enabled=forms.BooleanField(label=_("Hosting reminders enabled"),required=False)
    expires_at=forms.DateTimeField(label=_("Reported expiration (UTC)"),widget=forms.DateTimeInput(format="%Y-%m-%d %H:%M:%S"))
    recipient=forms.EmailField(label=_("Reminder recipient"))
    language=forms.ChoiceField(label=_("Reminder language"),choices=[("fr","Français"),("en","English")])
    action=forms.ChoiceField(label=_("Operator action"),choices=[("verify_expiration",_("Verify reported expiration")),("confirm_renewal",_("Report manual renewal"))])
    evidence_reference=forms.CharField(label=_("Verification reference"),max_length=500)
    reason=forms.CharField(label=_("Reason"),max_length=2000,widget=forms.Textarea)


class ReminderRetryForm(forms.Form):
    operation_id=forms.UUIDField(widget=forms.HiddenInput)
    expected_attempt_id=forms.UUIDField(widget=forms.HiddenInput)
    possible_duplicate_confirmed=forms.BooleanField(label=_("I acknowledge that this resend may duplicate an earlier email"),required=True)
    reason=forms.CharField(label=_("Reason"),max_length=2000,widget=forms.Textarea)


class HostingObservationForm(forms.Form):
    id=forms.UUIDField(widget=forms.HiddenInput)
    operation_id=forms.UUIDField(widget=forms.HiddenInput)
    observed_at=forms.DateTimeField(label=_("Observation time"),widget=forms.DateTimeInput(format="%Y-%m-%d %H:%M:%S"))
    installed_bytes=forms.IntegerField(label=_("Reported installed bytes"),min_value=0,max_value=9223372036854775807,required=False)
    temporary_peak_bytes=forms.IntegerField(label=_("Reported temporary peak bytes"),min_value=0,max_value=9223372036854775807,required=False)
    quota_used_bytes=forms.IntegerField(label=_("Reported quota used bytes"),min_value=0,max_value=9223372036854775807,required=False)
    quota_limit_bytes=forms.IntegerField(label=_("Reported quota limit bytes"),min_value=0,max_value=9223372036854775807,required=False)
    service_state=forms.ChoiceField(label=_("Reported service state"),choices=[("unknown",_("Unknown")),("up",_("Reported available")),("down",_("Reported unavailable"))])
    evidence_reference=forms.CharField(label=_("Verification reference"),max_length=500)
    reason=forms.CharField(label=_("Reason"),max_length=2000,widget=forms.Textarea)


class HostingReadOnlyAdmin(TechnicalReadOnlyAdmin):
    change_list_template="admin/backups/hosting_change_list.html"
    actions=None


@admin.register(HostingConfiguration)
class HostingConfigurationAdmin(HostingReadOnlyAdmin):
    list_display=["id","expires_at","recipient","enabled","version","updated_at"]
    readonly_fields=[field.name for field in HostingConfiguration._meta.fields if field.name!="singleton"]
    exclude=["singleton"]

    def get_urls(self):
        return [path("configure/",self.admin_site.admin_view(self.configure_view),name="backups_hostingconfiguration_configure")]+super().get_urls()

    def configure_view(self,request):
        if not technical(request):raise PermissionDenied()
        state=operator_status(request)
        initial={**state["configuration"],"operation_id":uuid.uuid4(),"expected_version":state["configuration"]["version"],"action":"verify_expiration"}
        if initial["expected_version"]==0:initial["enabled"]=True
        form=HostingForm(request.POST if request.method=="POST" else None,initial=initial)
        if request.method=="POST" and form.is_valid():
            try:change_hosting(request.user,form.cleaned_data)
            except APIException as error:
                if error.status_code in (401,403):raise PermissionDenied() from None
                form.add_error(None,_("Hosting configuration changed or is invalid. Reload and verify the reported date."))
                state=operator_status(request)
            else:
                self.message_user(request,_("Hosting report saved. No email or automatic renewal was performed."),level=messages.SUCCESS)
                return HttpResponseRedirect(reverse("admin:backups_hostingconfiguration_configure"))
        rows=[{**item,"label":STATE_LABELS[item["effective_state"]]} for item in state["reminders"]]
        return TemplateResponse(request,"admin/backups/hosting_configuration.html",{**self.admin_site.each_context(request),
            "title":_("Hosting and expiration"),"opts":self.model._meta,"form":form,"hosting":state,
            "service_label":{"up":_("Reported available"),"down":_("Reported unavailable"),"unknown":_("Unknown")}.get(
                state["reported_space"]["service_state"] if state["reported_space"] else "unknown"),
            "expiration_label":STATE_LABELS[state["expiration_state"]],"reminders":rows})


@admin.register(HostingConfigurationChange)
class HostingConfigurationChangeAdmin(HostingReadOnlyAdmin):
    list_display=["id","created_at","actor","action","previous_version","version","reason"]
    readonly_fields=[field.name for field in HostingConfigurationChange._meta.fields]
    def get_queryset(self,request):return super().get_queryset(request).select_related("actor","configuration")


@admin.register(HostingReminder)
class HostingReminderAdmin(HostingReadOnlyAdmin):
    list_display=["id","expires_at","lead_days","display_state","version"]
    readonly_fields=[field.name for field in HostingReminder._meta.fields]

    @admin.display(description=_("State"))
    def display_state(self,obj):return STATE_LABELS[obj.state]

    def get_urls(self):
        return [path("<uuid:reminder_id>/retry/",self.admin_site.admin_view(self.retry_view),name="backups_hostingreminder_retry")]+super().get_urls()

    def retry_view(self,request,reminder_id):
        from django.shortcuts import get_object_or_404
        if not technical(request):raise PermissionDenied()
        reminder=get_object_or_404(HostingReminder,id=reminder_id)
        form=ReminderRetryForm(request.POST if request.method=="POST" else None,initial={"operation_id":uuid.uuid4(),"expected_attempt_id":reminder.current_attempt_id})
        if request.method=="POST" and form.is_valid():
            try:retry_hosting(request.user,reminder.id,form.cleaned_data)
            except APIException as error:
                if error.status_code in (401,403):raise PermissionDenied() from None
                form.add_error(None,_("The reminder changed or cannot be resent. Inspect its current attempts."))
            else:
                self.message_user(request,_("Reminder attempt recorded. Inspect SMTP acceptance; inbox delivery is unconfirmed."),level=messages.SUCCESS)
                return HttpResponseRedirect(reverse("admin:backups_hostingreminderattempt_changelist"))
        return TemplateResponse(request,"admin/backups/hosting_retry.html",{**self.admin_site.each_context(request),
            "title":_("Explicit hosting reminder resend"),"opts":self.model._meta,"form":form,"reminder":reminder,
            "state_label":STATE_LABELS[reminder.state]})


@admin.register(HostingReminderAttempt)
class HostingReminderAttemptAdmin(HostingReadOnlyAdmin):
    list_display=["id","started_at","recipient","display_state","failure_code","number"]
    readonly_fields=[field.name for field in HostingReminderAttempt._meta.fields]
    @admin.display(description=_("State"))
    def display_state(self,obj):return STATE_LABELS[obj.state]
    def get_queryset(self,request):return super().get_queryset(request).select_related("actor","reminder")


@admin.register(HostingObservation)
class HostingObservationAdmin(HostingReadOnlyAdmin):
    list_display=["id","observed_at","actor","service_state","installed_bytes","quota_used_bytes","quota_limit_bytes"]
    readonly_fields=[field.name for field in HostingObservation._meta.fields]
    def get_queryset(self,request):return super().get_queryset(request).select_related("actor")

    def get_urls(self):
        return [path("record/",self.admin_site.admin_view(self.record_view),name="backups_hostingobservation_record")]+super().get_urls()

    def record_view(self,request):
        if not technical(request):raise PermissionDenied()
        form=HostingObservationForm(request.POST if request.method=="POST" else None,initial={
            "id":uuid.uuid4(),"operation_id":uuid.uuid4(),"observed_at":timezone.now(),"service_state":"unknown"})
        if request.method=="POST" and form.is_valid():
            try:record_hosting_observation(request.user,form.cleaned_data)
            except APIException as error:
                if error.status_code in (401,403):raise PermissionDenied() from None
                form.add_error(None,_("Observation could not be recorded. Check its date and values."))
            else:
                self.message_user(request,_("Observation recorded as a dated operator report."),level=messages.SUCCESS)
                return HttpResponseRedirect(reverse("admin:backups_hostingobservation_changelist"))
        return TemplateResponse(request,"admin/backups/hosting_observation.html",{**self.admin_site.each_context(request),
            "title":_("Hosting observation"),"opts":self.model._meta,"form":form})


@admin.register(HostingReminderRepair)
class HostingReminderRepairAdmin(HostingReadOnlyAdmin):
    list_display=["id","created_at","actor","attempt","gate_version","reason"]
    readonly_fields=[field.name for field in HostingReminderRepair._meta.fields]
    def get_queryset(self,request):return super().get_queryset(request).select_related("actor","attempt")
