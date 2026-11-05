from django.http import FileResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import mixins, serializers, viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from backups.models import BackupDispatch, BackupJob, BackupPart, SendAttempt
from backups.services.jobs import prepare_backup, verify_part_file
from backups.services.dispatch import start_dispatch, advance_dispatch, dispatch_progress
from backups.services.abandonment import abandon_backup
from backups.services.email_parts import BackupEmailError
from backups.services.sending import BackupSendUnavailable, send_part
from common.api.fields import StrictSerializer
from identity.services.authentication import DeviceJWTAuthentication
from identity.services.permissions import IsTechnicalSuperuser


class PrepareBackupInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    recipient = serializers.EmailField(max_length=254)


class BackupPartSerializer(serializers.ModelSerializer):
    class Meta:
        model = BackupPart
        fields = ["id", "number", "filename", "size_bytes", "sha256"]
        read_only_fields = fields


class BackupJobSerializer(serializers.ModelSerializer):
    parts = BackupPartSerializer(many=True, read_only=True)

    class Meta:
        model = BackupJob
        fields = ["id", "operation_id", "requested_by_id", "recipient", "reason", "state", "key_id",
            "snapshot_sha256", "snapshot_bytes", "archive_bytes", "part_count", "prepared_at", "failure_code",
            "created_at", "updated_at", "manifest", "parts", "cleanup_state", "cleanup_started_at", "cleaned_at", "cleanup_error_code", "abandoned_at", "abandoned_by_id", "abandon_operation_id", "abandon_reason"]
        read_only_fields = fields


class AbandonBackupInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000, allow_blank=False)


class SendPartInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    retry_of = serializers.UUIDField(required=False, allow_null=True)


class SendAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = SendAttempt
        fields = ["id", "part_id", "dispatch_id", "operation_id", "requested_by_id", "previous_id", "number", "retry_number",
            "state", "possible_duplicate", "sender", "recipient", "encoded_bytes", "wire_sha256", "created_at", "completed_at", "error_code"]
        read_only_fields = fields


class StartDispatchInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000, allow_blank=False)


class AdvanceDispatchInput(StrictSerializer):
    pass


class BackupDispatchSerializer(serializers.ModelSerializer):
    accepted_parts = serializers.SerializerMethodField()
    next_attempt_at = serializers.SerializerMethodField()

    def get_accepted_parts(self, obj) -> int:
        return dispatch_progress(obj)["accepted_parts"]

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_next_attempt_at(self, obj):
        value = dispatch_progress(obj)["next_attempt_at"]
        return serializers.DateTimeField().to_representation(value) if value else None

    class Meta:
        model = BackupDispatch
        fields = ["id", "job_id", "operation_id", "requested_by_id", "reason", "state", "created_at", "finished_at", "accepted_parts", "next_attempt_at"]
        read_only_fields = fields


class BackupArchiveCleaned(APIException):
    status_code = 410
    default_code = "backup_archive_cleaned"
    default_detail = {"code": "backup_archive_cleaned"}


class BackupViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    authentication_classes = [DeviceJWTAuthentication, SessionAuthentication]
    permission_classes = [IsTechnicalSuperuser]
    serializer_class = BackupJobSerializer
    queryset = BackupJob.objects.prefetch_related("parts").order_by("created_at", "id")

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action in {"start_dispatch", "advance_dispatch", "retrieve_dispatch"}:
            return queryset.prefetch_related(None)
        return queryset

    @extend_schema(request=PrepareBackupInput, responses={200: BackupJobSerializer, 201: BackupJobSerializer})
    def create(self, request, *args, **kwargs):
        serializer = PrepareBackupInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        job, created = prepare_backup(request.user, serializer.validated_data, getattr(request, "device_session", None))
        return Response(self.get_serializer(job).data, status=201 if created else 200)

    @extend_schema(responses={(200, "application/octet-stream"): bytes})
    def download_part(self, request, *args, **kwargs):
        job = self.get_object()
        part = get_object_or_404(job.parts, number=self.kwargs["number"])
        if job.cleanup_state == "cleaned":
            raise BackupArchiveCleaned()
        path = verify_part_file(job, part)
        return FileResponse(path.open("rb"), as_attachment=True, filename=part.filename, content_type="application/octet-stream")


    @extend_schema(request=SendPartInput, responses={200: SendAttemptSerializer, 201: SendAttemptSerializer})
    def send_part(self, request, *args, **kwargs):
        job = self.get_object()
        part = get_object_or_404(job.parts, number=self.kwargs["number"])
        serializer = SendPartInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            attempt, created = send_part(request.user, part.id, **serializer.validated_data,
                session=getattr(request, "device_session", None))
        except BackupEmailError:
            raise BackupSendUnavailable() from None
        return Response(SendAttemptSerializer(attempt).data, status=201 if created else 200)

    @extend_schema(responses=SendAttemptSerializer(many=True))
    def attempts(self, request, *args, **kwargs):
        job = self.get_object()
        part = get_object_or_404(job.parts, number=self.kwargs["number"])
        rows = part.send_attempts.order_by("created_at", "id")
        page = self.paginate_queryset(rows)
        if page is not None:
            return self.get_paginated_response(SendAttemptSerializer(page, many=True).data)
        return Response(SendAttemptSerializer(rows, many=True).data)


    @extend_schema(request=AbandonBackupInput,responses=BackupJobSerializer)
    def abandon(self,request,*args,**kwargs):
        job=self.get_object()
        serializer=AbandonBackupInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        result,_=abandon_backup(request.user,job.id,**serializer.validated_data,
            session=getattr(request,"device_session",None))
        return Response(BackupJobSerializer(result).data)


    @extend_schema(request=StartDispatchInput,responses={200:BackupDispatchSerializer,201:BackupDispatchSerializer})
    def start_dispatch(self,request,*args,**kwargs):
        job=self.get_object()
        serializer=StartDispatchInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        command,created=start_dispatch(request.user,job.id,**serializer.validated_data,session=getattr(request,"device_session",None))
        return Response(BackupDispatchSerializer(command).data,status=201 if created else 200)

    @extend_schema(request=AdvanceDispatchInput,responses=BackupDispatchSerializer)
    def advance_dispatch(self,request,*args,**kwargs):
        job=self.get_object()
        serializer=AdvanceDispatchInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            command=advance_dispatch(request.user,job.id,self.kwargs["dispatch_id"],session=getattr(request,"device_session",None))
        except BackupEmailError:
            raise BackupSendUnavailable() from None
        return Response(BackupDispatchSerializer(command).data)

    @extend_schema(responses=BackupDispatchSerializer)
    def retrieve_dispatch(self,request,*args,**kwargs):
        job=self.get_object()
        command=get_object_or_404(BackupDispatch.objects.select_related("job"),job=job,id=self.kwargs["dispatch_id"])
        return Response(BackupDispatchSerializer(command).data)
