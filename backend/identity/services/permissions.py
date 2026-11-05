from rest_framework.permissions import BasePermission

from identity.services.authentication import require_live_session
from identity.models import User


class IsTechnicalSuperuser(BasePermission):
    """Business ownership and staff status alone do not grant API documentation."""

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        session = getattr(request, "device_session", None)
        if session is not None:
            require_live_session(user, session.id, session.installation)
        return User.objects.filter(id=user.id, is_active=True, is_superuser=True).exists()
