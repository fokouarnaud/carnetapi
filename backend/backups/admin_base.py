"""Shared current-role policy for technical, readonly administration."""
from unfold.admin import ModelAdmin
from identity.models import User


def technical(request):
    expected=getattr(request,"restore_recovery_user_id",None)
    if expected is not None and str(request.user.id)!=str(expected):
        return False
    filters={"is_staff":True} if expected is not None else {}
    return User.objects.filter(id=request.user.id, is_active=True, is_superuser=True,**filters).exists()


class TechnicalReadOnlyAdmin(ModelAdmin):
    def has_module_permission(self, request):
        return technical(request)

    def has_view_permission(self, request, obj=None):
        return technical(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset if technical(request) else queryset.none()
