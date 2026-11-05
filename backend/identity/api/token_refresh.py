from datetime import datetime, timezone as dt_timezone
import uuid

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from identity.models import DeviceSession


class DeviceRefreshSerializer(TokenRefreshSerializer):
    refresh = serializers.CharField(max_length=4096)

    @transaction.atomic
    def validate(self, attrs):
        token = RefreshToken(attrs["refresh"])
        try:
            session_id = uuid.UUID(str(token.get("session_id", "")))
            user_id = uuid.UUID(str(token.get("user_id", "")))
        except (ValueError, TypeError, AttributeError):
            raise AuthenticationFailed("Session invalide.", code="invalid_session")

        now = timezone.now()
        sessions = DeviceSession.objects.filter(
            id=session_id, installation__user_id=user_id, installation__user__is_active=True,
            installation__revoked_at__isnull=True, revoked_at__isnull=True,
            expires_at__gt=now, refresh_jti=token["jti"],
        )
        if not sessions.exists():
            raise AuthenticationFailed("Session expirée ou retirée.", code="invalid_session")

        # Library rotation and blacklist writes participate in the same DB transaction.
        result = super().validate(attrs)
        replacement = RefreshToken(result["refresh"])
        updated = sessions.update(
            refresh_jti=replacement["jti"],
            expires_at=datetime.fromtimestamp(replacement["exp"], tz=dt_timezone.utc),
            updated_at=now,
        )
        if updated != 1:
            raise AuthenticationFailed("Renouvellement déjà utilisé.", code="stale_refresh")
        return result


class TokenPairSerializer(serializers.Serializer):
    access = serializers.CharField()
    refresh = serializers.CharField()


class DeviceTokenRefreshView(TokenRefreshView):
    serializer_class = DeviceRefreshSerializer

    @extend_schema(request=DeviceRefreshSerializer, responses={200: TokenPairSerializer})
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)
