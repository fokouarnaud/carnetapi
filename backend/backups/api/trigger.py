"""Opaque trigger authentication only grants a bounded scheduler tick."""
from secrets import compare_digest
from django.conf import settings
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import serializers
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from common.api.fields import StrictSerializer
from backups.services.scheduler_errors import SchedulerUnavailable


class TriggerPrincipal:
    is_authenticated = True
    is_superuser = False
    id = None


class BackupTriggerAuthentication(BaseAuthentication):
    def authenticate_header(self, request):
        return "Bearer"

    def authenticate(self, request):
        token = getattr(settings, "BACKUP_SCHEDULER_TOKEN", "")
        encryption_key = getattr(settings, "BACKUP_ENCRYPTION_KEY", "")
        if isinstance(encryption_key, bytes):
            encryption_key = encryption_key.decode("ascii", errors="replace")
        forbidden = [settings.SECRET_KEY, settings.SIMPLE_JWT.get("SIGNING_KEY", ""),
            getattr(settings, "JWT_SIGNING_KEY", ""), getattr(settings, "EMAIL_HOST_PASSWORD", ""), encryption_key]
        if (not isinstance(token, str) or not 32 <= len(token) <= 256 or not token.isascii()
                or any(character.isspace() for character in token) or token in forbidden):
            raise SchedulerUnavailable()
        if settings.APP_ENV == "production" and not request.is_secure():
            raise SchedulerUnavailable()
        supplied = get_authorization_header(request)
        if len(supplied) > 300 or not compare_digest(supplied, ("Bearer " + token).encode("ascii")):
            raise AuthenticationFailed()
        return TriggerPrincipal(), "backup-trigger"


class BackupTriggerScheme(OpenApiAuthenticationExtension):
    target_class = BackupTriggerAuthentication
    name = "BackupTrigger"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "opaque"}


class TriggerInput(StrictSerializer):
    pass


class TriggerOutput(serializers.Serializer):
    status = serializers.ChoiceField(choices=("idle","disabled","blocked","busy","prepared","progressed","accepted","incomplete"))


class TriggerAuthenticationError(serializers.Serializer):
    code = serializers.CharField()
    detail = serializers.CharField()


class TriggerUnavailable(serializers.Serializer):
    code = serializers.ChoiceField(choices=("backup_scheduler_unavailable",))


class BackupTriggerView(APIView):
    authentication_classes = [BackupTriggerAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(request={"application/json":{"type":"object","additionalProperties":False}},responses={
        200:OpenApiResponse(TriggerOutput,description=("One bounded step. idle/disabled: no due capture; busy: another tick owns the scheduler lease; "
            "blocked: explicit intervention required; prepared/progressed: call again; accepted: SMTP acceptance; incomplete: retries exhausted. "
            "These states are not proof of delivery to an inbox.")),
        400:OpenApiResponse({"type":"object","additionalProperties":{}},description="Invalid body. No account, recipient, schedule, or backup identifier may be supplied."),
        401:OpenApiResponse(TriggerAuthenticationError,description="Missing or incorrect dedicated trigger secret. A user JWT grants no trigger access."),
        503:OpenApiResponse(TriggerUnavailable,description=("Scheduler unavailable: invalid configuration, HTTPS required in production, "
            "current technical account unavailable, or the worker cannot acquire its normal database lease. Retry without resetting persistent run state."))},
        description=("Execute one daily capture or one bounded delivery step using the current configured technical account. "
            "The dedicated opaque bearer secret grants only this action, never backup listing, downloads or restoration. "
            "The body is empty; authority and recipient come from server configuration. Both HTTP and console scheduler steps "
            "hold the external maintenance barrier until SQL, SMTP and cleanup finish and ORM connections close. "
            "Late invocations capture current data once and journal missed days; incomplete archives suspend new automatic captures. "
            "SMTP retries remain limited to three additional attempts at least fifteen minutes apart."))
    def post(self, request):
        from backups.services.scheduler import scheduler_tick
        serializer = TriggerInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response({"status": scheduler_tick()})
