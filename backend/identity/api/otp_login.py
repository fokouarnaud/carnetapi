"""US-01: anonymous OTP request/verify; the only path that issues a first device session."""
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from common.api.fields import StrictSerializer
from identity.services import otp
from identity.services.authentication import require_live_session

REQUEST_ERRORS = {
    "otp_request_invalid": status.HTTP_400_BAD_REQUEST,
    "otp_operation_conflict": status.HTTP_409_CONFLICT,
    "otp_account_exists": status.HTTP_409_CONFLICT,
    "otp_account_unknown": status.HTTP_404_NOT_FOUND,
    "sms_daily_quota_reached": status.HTTP_429_TOO_MANY_REQUESTS,
    "otp_send_uncertain": status.HTTP_409_CONFLICT,
    "sms_budget_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "sms_transport_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "otp_recovery_unknown": status.HTTP_404_NOT_FOUND,
    "otp_phone_taken": status.HTTP_409_CONFLICT,
}
VERIFY_ERRORS = {
    "otp_code_malformed": status.HTTP_400_BAD_REQUEST,
    "otp_code_invalid": status.HTTP_400_BAD_REQUEST,
    "otp_challenge_closed": status.HTTP_410_GONE,
    "otp_account_exists": status.HTTP_409_CONFLICT,
    "otp_account_unknown": status.HTTP_404_NOT_FOUND,
    "otp_phone_taken": status.HTTP_409_CONFLICT,
}


class OtpRequestInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    phone = serializers.CharField(max_length=16)
    purpose = serializers.CharField(max_length=20)
    language = serializers.ChoiceField(choices=["fr", "en"], required=False, default="fr")
    # Recovery only (US-07): the lost number and one unused recovery code.
    old_phone = serializers.CharField(max_length=16, required=False, default=None)
    recovery_code = serializers.CharField(max_length=20, required=False, default=None)


class OtpRequestOutput(serializers.Serializer):
    challenge_id = serializers.UUIDField()
    expires_at = serializers.DateTimeField()


class OtpVerifyInput(StrictSerializer):
    challenge_id = serializers.UUIDField()
    code = serializers.CharField(max_length=6)
    installation_label = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")


class OtpVerifyOutput(serializers.Serializer):
    user_id = serializers.UUIDField()
    installation_id = serializers.UUIDField()
    access = serializers.CharField()
    refresh = serializers.CharField()


def _error(error, table):
    body = {"code": error.code}
    for key, value in error.details.items():
        if value is not None:
            body[key] = value.isoformat() if hasattr(value, "isoformat") else value
    return Response(body, status=table[error.code])


class OtpRequestView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(request=OtpRequestInput, responses={202: OtpRequestOutput})
    def post(self, request):
        data = OtpRequestInput(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            challenge = otp.request_code(**data.validated_data)
        except otp.OtpError as error:
            return _error(error, REQUEST_ERRORS)
        return Response({"challenge_id": str(challenge.id), "expires_at": challenge.expires_at.isoformat()},
                        status=status.HTTP_202_ACCEPTED)


class OtpVerifyView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(request=OtpVerifyInput, responses={200: OtpVerifyOutput})
    def post(self, request):
        data = OtpVerifyInput(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            activation = otp.verify_code(**data.validated_data)
        except otp.OtpError as error:
            return _error(error, VERIFY_ERRORS)
        return Response({"user_id": str(activation.user.id), "installation_id": str(activation.installation.id),
                         "access": activation.access, "refresh": activation.refresh})


class RecoveryCodesOutput(serializers.Serializer):
    codes = serializers.ListField(child=serializers.CharField())


class RecoveryCodesView(APIView):
    """Authenticated holder only; codes are shown once and replace any unused earlier batch."""

    @extend_schema(request=None, responses={201: RecoveryCodesOutput})
    def post(self, request):
        session = request.device_session
        require_live_session(request.user, session.id, session.installation)
        return Response({"codes": otp.generate_recovery_codes(request.user)}, status=status.HTTP_201_CREATED)
