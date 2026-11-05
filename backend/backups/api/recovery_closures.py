"""Technical preview and explicit partial closure of the current recovery."""
from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import serializers
from rest_framework.authentication import SessionAuthentication
from rest_framework.response import Response
from rest_framework.views import APIView

from backups.api.recovery_devices import RecoveryDeviceAccessError
from backups.services.recovery_closures import RecoveryClosureInput, preview_recovery_closure, close_recovery
from common.services.errors import OperationConflict
from identity.services.authentication import DeviceJWTAuthentication
from identity.services.permissions import IsTechnicalSuperuser


class ClosureDeviceObservation(serializers.Serializer):
    id = serializers.UUIDField()
    shop_id = serializers.UUIDField()
    origin_installation_id = serializers.UUIDField()
    origin_user_id = serializers.UUIDField()


class RecoveryClosurePreview(serializers.Serializer):
    snapshot = serializers.JSONField(help_text="Observed inventory and review counts; no count proves exhaustive recovery.")
    preview_token = serializers.CharField()
    can_close_partial = serializers.BooleanField()
    can_close_complete = serializers.BooleanField()
    exhaustive_recovery_guaranteed = serializers.BooleanField()


class RecoveryClosureReceipt(serializers.Serializer):
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    restore_session_id = serializers.UUIDField()
    generation_id = serializers.UUIDField()
    state = serializers.ChoiceField(choices=["closed_partial"])
    kind = serializers.ChoiceField(choices=["partial"])
    closed_by = serializers.UUIDField()
    closed_installation_id = serializers.UUIDField(allow_null=True)
    closed_at = serializers.DateTimeField()
    reason = serializers.CharField()
    missing_devices = ClosureDeviceObservation(many=True)
    snapshot = serializers.JSONField()
    exhaustive_recovery_guaranteed = serializers.BooleanField()


ERRORS = {
    400: OpenApiResponse({"type": "object", "additionalProperties": True}, description="Invalid declaration or complete recovery not proven."),
    401: OpenApiResponse(RecoveryDeviceAccessError, description="Missing, expired or withdrawn current authentication."),
    403: OpenApiResponse(RecoveryDeviceAccessError, description="Current technical superadministrator required."),
    409: OpenApiResponse(RecoveryDeviceAccessError, description="Changed preview, current generation, operation UUID or closure declaration."),
}


class TechnicalRecoveryView(APIView):
    authentication_classes = [DeviceJWTAuthentication, SessionAuthentication]
    permission_classes = [IsTechnicalSuperuser]


class RecoveryClosurePreviewView(TechnicalRecoveryView):
    @extend_schema(responses={200: RecoveryClosurePreview, **ERRORS}, description=(
        "Preview the current generation's observed devices and captured decisions before a technical declaration. "
        "A journal deposit is not a business validation or proof of exhaustive phone recovery."))
    def get(self, request, session_id):
        return Response(preview_recovery_closure(actor=request.user, restore_session_id=session_id,
            session=getattr(request, "device_session", None)))


class RecoveryClosureView(TechnicalRecoveryView):
    @extend_schema(request=RecoveryClosureInput, responses={200: RecoveryClosureReceipt, 201: RecoveryClosureReceipt, **ERRORS}, description=(
        "Record a motivated partial closure with an explicit list of current device observations declared unavailable. "
        "The frozen snapshot and missing-data warning persist. Exact retries return the first receipt. "
        "A complete closure remains refused until reliable device reconciliation is implemented. "
        "New late journals remain depositable without business effects; new integration requires the unresolved reopening/reconciliation policy. "
        "No account, session, access right, stock movement or cash entry is restored by this declaration."))
    def post(self, request, session_id):
        incoming = RecoveryClosureInput(data=request.data)
        incoming.is_valid(raise_exception=True)
        if incoming.validated_data["restore_session_id"] != session_id:
            raise OperationConflict()
        receipt, created = close_recovery(actor=request.user, data=request.data,
            session=getattr(request, "device_session", None))
        return Response(receipt, status=201 if created else 200)
