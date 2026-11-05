"""Explicit operator opt-in; these references contain no recovery code or password."""
from pathlib import Path
import uuid
from django.core.exceptions import ImproperlyConfigured


def recovery_web_options(environment):
    names=("APP_RESTORE_RECOVERY_VAULT","APP_RESTORE_RECOVERY_BOOTSTRAP_OPERATION","APP_RESTORE_RECOVERY_GATE_VERSION")
    values=[environment.get(name) for name in names]
    if not any(values):
        return None
    try:
        if not all(values):
            raise ValueError()
        version=int(values[2])
        if version<1:
            raise ValueError()
        return {"vault":Path(values[0]),"bootstrap_operation":str(uuid.UUID(values[1])),"gate_version":version}
    except (ValueError,TypeError,AttributeError):
        raise ImproperlyConfigured("Recovery interface configuration is incomplete or invalid.") from None
