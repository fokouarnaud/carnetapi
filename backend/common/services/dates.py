"""A local calendar deadline remains a UTC instant in durable data."""
from datetime import datetime, time, timezone as utc
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rest_framework.exceptions import ValidationError


def due_at_utc(local_date, timezone_name):
    if local_date is None:
        return None
    try:
        return datetime.combine(local_date, time.min, tzinfo=ZoneInfo(timezone_name)).astimezone(utc.utc)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError({"due_date": "Fuseau de boutique invalide."})
    except OverflowError:
        raise ValidationError({"due_date": "Échéance hors de la plage UTC représentable."})
