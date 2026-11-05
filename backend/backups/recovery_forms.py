"""Keep the normal Unfold login and restrict a recovery session to its attested user."""
from django.core.exceptions import ValidationError
from unfold.forms import AuthenticationForm


class RecoveryAuthenticationForm(AuthenticationForm):
    def confirm_login_allowed(self,user):
        super().confirm_login_allowed(user)
        expected=getattr(self.request,"restore_recovery_user_id",None)
        if expected is not None and (str(user.id)!=str(expected) or not user.is_superuser):
            raise ValidationError(self.error_messages["invalid_login"],code="invalid_login",
                params={"username":self.username_field.verbose_name})
