"""Current technical authority manages reports; an opaque token only triggers a tick."""
from datetime import timedelta
from secrets import compare_digest
from django.conf import settings
from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.utils import extend_schema, OpenApiResponse, extend_schema_serializer
from rest_framework import serializers, viewsets
from rest_framework.authentication import BaseAuthentication, SessionAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from common.api.fields import StrictSerializer, ExactIntegerField, StrictBoolean
from common.services.parsers import BoundedJSONParser
from identity.services.authentication import DeviceJWTAuthentication
from identity.services.permissions import IsTechnicalSuperuser
from backups.models import HostingReminder, HostingReminderAttempt, HostingObservation
from backups.services.jobs import authorize
from backups.services.hosting import (HostingUnavailable, DOUALA, configuration_data, change_hosting, hosting_status,
    hosting_tick, reminder_data, attempt_data, retry_hosting, record_hosting_observation)


class HostingConfigurationInput(StrictSerializer):
    operation_id=serializers.UUIDField()
    expected_version=ExactIntegerField(min_value=0,max_value=9223372036854775806)
    enabled=StrictBoolean()
    expires_at=serializers.DateTimeField()
    recipient=serializers.EmailField(max_length=254)
    language=serializers.ChoiceField(choices=["fr","en"])
    action=serializers.ChoiceField(choices=["verify_expiration","confirm_renewal"])
    evidence_reference=serializers.CharField(max_length=500,allow_blank=False)
    reason=serializers.CharField(max_length=2000,allow_blank=False)

    def validate_expires_at(self,value):
        try:value.astimezone(DOUALA).date()-timedelta(days=7)
        except (ValueError,OverflowError):raise serializers.ValidationError("expiration_out_of_range") from None
        return value


class HostingConfigurationOutput(serializers.Serializer):
    id=serializers.UUIDField(allow_null=True)
    version=serializers.IntegerField()
    enabled=serializers.BooleanField()
    expires_at=serializers.DateTimeField(allow_null=True)
    recipient=serializers.CharField(allow_blank=True)
    language=serializers.ChoiceField(choices=["fr","en"])
    evidence_reference=serializers.CharField(allow_blank=True)
    timezone_name=serializers.CharField()
    updated_at=serializers.DateTimeField(allow_null=True)
    updated_by_id=serializers.UUIDField(allow_null=True)
    verified_generation_id=serializers.UUIDField(allow_null=True)
    last_reported_renewal_at=serializers.DateTimeField(allow_null=True)
    renewal_source=serializers.ChoiceField(choices=["operator_report"])


class HostingRetryInput(StrictSerializer):
    operation_id=serializers.UUIDField()
    expected_attempt_id=serializers.UUIDField()
    possible_duplicate_confirmed=StrictBoolean()
    reason=serializers.CharField(max_length=2000,allow_blank=False)


class HostingObservationInput(StrictSerializer):
    id=serializers.UUIDField()
    operation_id=serializers.UUIDField()
    observed_at=serializers.DateTimeField()
    installed_bytes=ExactIntegerField(min_value=0,max_value=9223372036854775807,allow_null=True,default=None)
    temporary_peak_bytes=ExactIntegerField(min_value=0,max_value=9223372036854775807,allow_null=True,default=None)
    quota_used_bytes=ExactIntegerField(min_value=0,max_value=9223372036854775807,allow_null=True,default=None)
    quota_limit_bytes=ExactIntegerField(min_value=0,max_value=9223372036854775807,allow_null=True,default=None)
    service_state=serializers.ChoiceField(choices=["up","down","unknown"])
    evidence_reference=serializers.CharField(max_length=500,allow_blank=False)
    reason=serializers.CharField(max_length=2000,allow_blank=False)

    def validate_observed_at(self,value):
        if value>timezone.now():raise serializers.ValidationError("future_observation_not_allowed")
        return value


class HostingReminderOutput(serializers.Serializer):
    id=serializers.UUIDField()
    expires_at=serializers.DateTimeField()
    lead_days=serializers.ChoiceField(choices=[2,7])
    due_local_date=serializers.DateField()
    state=serializers.ChoiceField(choices=["pending","sending","accepted","failed","uncertain"])
    effective_state=serializers.ChoiceField(choices=["pending","missed","sending","accepted","failed","uncertain"])
    version=serializers.IntegerField()
    current_attempt_id=serializers.UUIDField(allow_null=True)


class HostingAttemptOutput(serializers.Serializer):
    id=serializers.UUIDField()
    reminder_id=serializers.UUIDField()
    number=serializers.IntegerField()
    command_id=serializers.UUIDField(allow_null=True)
    actor_id=serializers.UUIDField()
    configuration_version=serializers.IntegerField()
    recipient=serializers.EmailField()
    language=serializers.ChoiceField(choices=["fr","en"])
    message_id=serializers.CharField()
    state=serializers.ChoiceField(choices=["sending","accepted","failed","uncertain"])
    started_at=serializers.DateTimeField()
    finished_at=serializers.DateTimeField(allow_null=True)
    failure_code=serializers.CharField(allow_blank=True)
    reason=serializers.CharField(allow_blank=True)


class HostingObservationOutput(serializers.Serializer):
    id=serializers.UUIDField()
    operation_id=serializers.UUIDField()
    actor_id=serializers.UUIDField()
    observed_at=serializers.DateTimeField()
    recorded_at=serializers.DateTimeField()
    installed_bytes=serializers.IntegerField(allow_null=True)
    temporary_peak_bytes=serializers.IntegerField(allow_null=True)
    quota_used_bytes=serializers.IntegerField(allow_null=True)
    quota_limit_bytes=serializers.IntegerField(allow_null=True)
    service_state=serializers.ChoiceField(choices=["up","down","unknown"])
    evidence_reference=serializers.CharField()
    reason=serializers.CharField()


class HostingSpaceOutput(serializers.Serializer):
    id=serializers.UUIDField()
    observed_at=serializers.DateTimeField()
    recorded_at=serializers.DateTimeField()
    installed_bytes=serializers.CharField(allow_null=True)
    temporary_peak_bytes=serializers.CharField(allow_null=True)
    quota_used_bytes=serializers.CharField(allow_null=True)
    quota_limit_bytes=serializers.CharField(allow_null=True)
    quota_remaining_bytes=serializers.CharField(allow_null=True)
    installed_budget_state=serializers.ChoiceField(choices=["unknown","reported_below_limit","reported_limit_exceeded"])
    service_state=serializers.ChoiceField(choices=["up","down","unknown"])
    evidence_reference=serializers.CharField()


class HostingBackupStatus(serializers.Serializer):
    blocked=serializers.BooleanField()
    blocked_count=serializers.IntegerField()
    last_accepted_at=serializers.DateTimeField(allow_null=True)
    last_accepted_age_seconds=serializers.IntegerField(allow_null=True)
    clock_inconsistent=serializers.BooleanField()


@extend_schema_serializer(many=False)
class HostingStatusOutput(serializers.Serializer):
    checked_at=serializers.DateTimeField()
    expiration_state=serializers.ChoiceField(choices=["unknown","active","expiring","expired"])
    configuration_state=serializers.ChoiceField(choices=["unconfigured","disabled","clock_inconsistent","awaiting_verification","verified"])
    configuration=HostingConfigurationOutput()
    reminders=HostingReminderOutput(many=True)
    reported_space=HostingSpaceOutput(allow_null=True)
    filesystem_free_bytes=serializers.CharField(allow_null=True)
    filesystem_measurement_scope=serializers.CharField()
    backup=HostingBackupStatus()
    requires_attention=serializers.BooleanField()


ERRORS={code:{"type":"object","additionalProperties":True} for code in (400,401,403,404,409,415)}


class HostingUnavailableOutput(serializers.Serializer):
    code=serializers.ChoiceField(choices=["hosting_reminder_unavailable"])


class HostingTechnicalView(APIView):
    authentication_classes=[DeviceJWTAuthentication,SessionAuthentication]
    permission_classes=[IsTechnicalSuperuser]
    parser_classes=[BoundedJSONParser]


class HostingConfigurationView(HostingTechnicalView):
    @extend_schema(responses={200:HostingConfigurationOutput,**ERRORS},description=(
        "Current technical superuser reads reported expiration and reminder configuration. No settings or SMTP "
        "attempt is created by GET. Unknown expiration is null, never an assumed renewal."))
    def get(self,request):
        authorize(request.user,getattr(request,"device_session",None))
        return Response(configuration_data())

    @extend_schema(request=HostingConfigurationInput,responses={200:HostingConfigurationOutput,**ERRORS},description=(
        "Versioned operator report with mandatory reason and verification reference. verify_expiration records a "
        "date/recipient without claiming a renewal. confirm_renewal explicitly reports a manually performed renewal "
        "and requires a later future expiry. No host API or email is called. Exact retries return their historical "
        "receipt after current authorization. A restored generation needs fresh technical confirmation."))
    def patch(self,request):
        return Response(change_hosting(request.user,request.data,getattr(request,"device_session",None)))


class HostingStatusView(HostingTechnicalView):
    @extend_schema(responses={200:HostingStatusOutput,**ERRORS},description=(
        "Technical status of reported expiration, reminder SMTP attempts, dated operator measurements/outages and "
        "actual database-filesystem free space. Filesystem free space is not provider quota. The strict 500000000-byte "
        "installed budget is classified only as reported, not deployment qualification. Backup acceptance age and "
        "incomplete archives stay separate from inbox delivery and hosting availability."))
    def get(self,request):
        return Response(hosting_status(request.user,getattr(request,"device_session",None)))


class HostingReminderViewSet(viewsets.GenericViewSet):
    queryset=None
    authentication_classes=[DeviceJWTAuthentication,SessionAuthentication]
    permission_classes=[IsTechnicalSuperuser]
    parser_classes=[BoundedJSONParser]
    serializer_class=HostingReminderOutput

    @extend_schema(responses={200:HostingReminderOutput(many=True),**ERRORS},description="Current technical superuser reads reminder history, SQL pages 25/100, expiry/day/UUID order. Expired unsent windows are missed, not accepted.")
    def list(self,request,*args,**kwargs):
        authorize(request.user,getattr(request,"device_session",None))
        page=self.paginate_queryset(HostingReminder.objects.order_by("expires_at","lead_days","id"))
        now=timezone.now()
        return self.get_paginated_response([reminder_data(row,now) for row in page])

    @extend_schema(responses={200:HostingAttemptOutput(many=True),**ERRORS},description="Current technical superuser reads immutable SMTP evidence; accepted means SMTP acceptance, failed or uncertain remains visible. SQL pages 25/100, attempt number/UUID order.")
    def attempts(self,request,*args,**kwargs):
        from django.shortcuts import get_object_or_404
        authorize(request.user,getattr(request,"device_session",None))
        reminder=get_object_or_404(HostingReminder,id=kwargs["pk"])
        page=self.paginate_queryset(HostingReminderAttempt.objects.filter(reminder=reminder).order_by("number","id"))
        return self.get_paginated_response([attempt_data(row) for row in page])

    @extend_schema(request=HostingRetryInput,responses={200:HostingAttemptOutput,201:HostingAttemptOutput,**ERRORS,
        503:OpenApiResponse(HostingUnavailableOutput,description="The worker cannot acquire its normal maintenance lease.")},description=(
        "Explicit technical resend of a failed/uncertain attempt, current expiry/configuration only. Requires the "
        "previous attempt UUID, a reason, a fresh command UUID and acknowledgement of a possible duplicate. "
        "A sending attempt cannot be superseded here. Automatic ticks never retry an uncertain outcome."))
    def retry(self,request,*args,**kwargs):
        receipt,created=retry_hosting(request.user,kwargs["pk"],request.data,getattr(request,"device_session",None))
        return Response(receipt,status=201 if created else 200)


class HostingObservationViewSet(viewsets.GenericViewSet):
    queryset=None
    authentication_classes=[DeviceJWTAuthentication,SessionAuthentication]
    permission_classes=[IsTechnicalSuperuser]
    parser_classes=[BoundedJSONParser]
    serializer_class=HostingObservationOutput

    @extend_schema(responses={200:HostingObservationOutput(many=True),**ERRORS},description=(
        "Dated operator reports, SQL pages 25/100 in observation time/UUID order. Observations preserve a reported "
        "outage after service recovers; their values are not a live provider measurement or proof of budget compliance."))
    def list(self,request,*args,**kwargs):
        authorize(request.user,getattr(request,"device_session",None))
        page=self.paginate_queryset(HostingObservation.objects.order_by("observed_at","id"))
        return self.get_paginated_response([row.receipt for row in page])

    @extend_schema(request=HostingObservationInput,responses={200:HostingObservationOutput,201:HostingObservationOutput,**ERRORS},description=(
        "Record an operator's dated availability/space observation with source reference and reason. Unknown byte "
        "counts remain null. Over-quota reality can be recorded without inventing free space. No hosting renewal, "
        "purchase, upgrade or automatic cleanup is performed."))
    def create(self,request,*args,**kwargs):
        receipt,created=record_hosting_observation(request.user,request.data,getattr(request,"device_session",None))
        return Response(receipt,status=201 if created else 200)


class HostingTriggerPrincipal:
    is_authenticated=True
    is_superuser=False
    id=None


class HostingTriggerAuthentication(BaseAuthentication):
    def authenticate_header(self,request):return "Bearer"

    def authenticate(self,request):
        token=getattr(settings,"HOSTING_REMINDER_TOKEN","")
        forbidden=[settings.SECRET_KEY,settings.SIMPLE_JWT.get("SIGNING_KEY",""),getattr(settings,"EMAIL_HOST_PASSWORD",""),
            getattr(settings,"BACKUP_SCHEDULER_TOKEN",""),getattr(settings,"BACKUP_ENCRYPTION_KEY","")]
        forbidden=[value.decode("ascii",errors="replace").strip() if isinstance(value,bytes) else str(value).strip() for value in forbidden]
        if (not isinstance(token,str) or not 32<=len(token)<=256 or not token.isascii()
                or any(char.isspace() for char in token) or token in forbidden):raise HostingUnavailable()
        if settings.APP_ENV=="production" and not request.is_secure():raise HostingUnavailable()
        supplied=get_authorization_header(request)
        if len(supplied)>300 or not compare_digest(supplied,("Bearer "+token).encode("ascii")):raise AuthenticationFailed()
        return HostingTriggerPrincipal(),"hosting-trigger"


class HostingTriggerScheme(OpenApiAuthenticationExtension):
    target_class=HostingTriggerAuthentication
    name="HostingTrigger"
    def get_security_definition(self,auto_schema):return {"type":"http","scheme":"bearer","bearerFormat":"opaque"}


class HostingTriggerInput(StrictSerializer):
    pass


class HostingTriggerOutput(serializers.Serializer):
    status=serializers.ChoiceField(choices=["unconfigured","disabled","idle","busy","accepted","failed","uncertain","clock_inconsistent","awaiting_verification"])


class HostingTriggerView(APIView):
    authentication_classes=[HostingTriggerAuthentication]
    permission_classes=[IsAuthenticated]
    parser_classes=[BoundedJSONParser]

    @extend_schema(request=HostingTriggerInput,responses={200:HostingTriggerOutput,**ERRORS,
        503:OpenApiResponse(HostingUnavailableOutput,description="Configuration, current technical actor, HTTPS or maintenance barrier unavailable.")},description=(
        "One bounded reminder step with an empty JSON body and a dedicated opaque secret. At the first invocation "
        "on J-7 or J-2 in Africa/Douala, one attempt is claimed durably before SMTP. Later automatic calls do not "
        "resend accepted/failed/uncertain attempts. This grants no configuration, backup or business access. "
        "The normal maintenance barrier remains held through SMTP and ORM closure. Acceptance is not inbox delivery."))
    def post(self,request):
        incoming=HostingTriggerInput(data=request.data);incoming.is_valid(raise_exception=True)
        return Response({"status":hosting_tick()})
