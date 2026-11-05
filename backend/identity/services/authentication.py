from django.utils.translation import gettext_lazy as _
import uuid

from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

from identity.models import DeviceSession


def require_live_session(user, session_id, installation=None):
    sessions = DeviceSession.objects.select_related("installation").filter(
        id=session_id, installation__user_id=user.id, installation__user__is_active=True,
        installation__revoked_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now(),
    )
    if installation is not None:
        sessions = sessions.filter(installation=installation)
    session = sessions.first()
    if session is None:
        raise AuthenticationFailed(_("Session expirée ou retirée."), code="invalid_session")
    return session


class DeviceJWTAuthentication(JWTAuthentication):
    """A valid signature is insufficient: the recorded installation must be live."""

    def authenticate(self, request):
        authenticated = super().authenticate(request)
        if authenticated is None:
            return None
        user, token = authenticated
        try:
            session_id = uuid.UUID(str(token.get("session_id", "")))
        except (ValueError, TypeError, AttributeError):
            raise AuthenticationFailed("Session invalide.", code="invalid_session")
        session = require_live_session(user, session_id)
        request.device_session = session
        return user, token
