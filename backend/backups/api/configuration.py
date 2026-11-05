"""Technical-only daily configuration; no scheduling secret or SMTP password is exposed."""
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.authentication import SessionAuthentication
from rest_framework.response import Response
from rest_framework.views import APIView
from common.api.fields import StrictSerializer, ExactIntegerField, StrictBoolean
from identity.services.authentication import DeviceJWTAuthentication
from identity.services.permissions import IsTechnicalSuperuser
from backups.services.jobs import authorize
from backups.services.configuration import configuration_snapshot, change_configuration


class BackupConfigurationOutput(serializers.Serializer):
    id = serializers.UUIDField()
    enabled = serializers.BooleanField()
    local_hour = serializers.IntegerField()
    local_minute = serializers.IntegerField()
    timezone_name = serializers.CharField()
    version = serializers.IntegerField()
    updated_at = serializers.DateTimeField(allow_null=True)
    updated_by_id = serializers.UUIDField(allow_null=True)
    automatic_recipient = serializers.CharField(allow_blank=True)
    next_run_at = serializers.DateTimeField(allow_null=True)


class BackupConfigurationInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    expected_version = ExactIntegerField(min_value=0)
    enabled = StrictBoolean()
    local_hour = ExactIntegerField(min_value=0, max_value=23)
    local_minute = ExactIntegerField(min_value=0, max_value=59)
    reason = serializers.CharField(max_length=2000, allow_blank=False)


class BackupConfigurationView(APIView):
    authentication_classes = [DeviceJWTAuthentication, SessionAuthentication]
    permission_classes = [IsTechnicalSuperuser]

    @extend_schema(responses=BackupConfigurationOutput)
    def get(self, request):
        authorize(request.user, getattr(request, "device_session", None))
        return Response(configuration_snapshot())

    @extend_schema(request=BackupConfigurationInput, responses=BackupConfigurationOutput)
    def patch(self, request):
        serializer = BackupConfigurationInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(change_configuration(request.user, **serializer.validated_data, session=getattr(request, "device_session", None)))
