"""Daily civil-time configuration and UTC previews, without executing a scheduler."""
from datetime import datetime, time, timedelta, timezone as datetime_timezone
from zoneinfo import ZoneInfo
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from backups.models import BackupConfiguration, BackupConfigurationChange
from backups.services.jobs import authorize
from common.services.errors import OperationConflict

DOUALA = ZoneInfo("Africa/Douala")


def current_configuration():
    return BackupConfiguration.objects.filter(singleton=1).first() or BackupConfiguration(version=0)


def next_daily_run(configuration, at):
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("aware_datetime_required")
    if not configuration.enabled:
        return None
    local = at.astimezone(DOUALA)
    candidate = datetime.combine(local.date(), time(configuration.local_hour, configuration.local_minute), tzinfo=DOUALA)
    if candidate < local:
        candidate += timedelta(days=1)
    return candidate.astimezone(datetime_timezone.utc)


def configuration_snapshot(configuration=None, at=None):
    configuration = configuration if configuration is not None else current_configuration()
    due = next_daily_run(configuration, at if at is not None else timezone.now())
    return {"id": str(configuration.id), "enabled": configuration.enabled,
        "local_hour": configuration.local_hour, "local_minute": configuration.local_minute,
        "timezone_name": configuration.timezone_name, "version": configuration.version,
        "updated_at": configuration.updated_at.isoformat() if configuration.updated_at else None,
        "updated_by_id": str(configuration.updated_by_id) if configuration.updated_by_id else None,
        "automatic_recipient": settings.EMAIL_HOST_USER, "next_run_at": due.isoformat() if due else None}


def change_configuration(actor, operation_id, expected_version, enabled, local_hour, local_minute, reason, session=None):
    with transaction.atomic():
        authorize(actor, session)
        if (type(expected_version) is not int or expected_version < 0 or type(enabled) is not bool
                or type(local_hour) is not int or not 0 <= local_hour <= 23
                or type(local_minute) is not int or not 0 <= local_minute <= 59
                or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000):
            raise ValidationError({"code": "invalid_backup_configuration"})
        reason = reason.strip()
        payload = {"expected_version": expected_version, "enabled": enabled, "local_hour": local_hour,
            "local_minute": local_minute, "reason": reason}
        previous = BackupConfigurationChange.objects.filter(operation_id=operation_id).first()
        if previous is not None:
            if previous.actor_id != actor.id or previous.payload != payload:
                raise OperationConflict()
            return previous.after
        configuration = current_configuration()
        if configuration.version != expected_version:
            raise OperationConflict()
        now = timezone.now()
        before = configuration_snapshot(configuration, at=now)
        configuration.version += 1
        configuration.enabled, configuration.local_hour, configuration.local_minute = enabled, local_hour, local_minute
        configuration.updated_by = actor
        configuration.save()
        after = configuration_snapshot(configuration, at=now)
        BackupConfigurationChange.objects.create(configuration=configuration, operation_id=operation_id, actor=actor,
            reason=reason, previous_version=expected_version, version=configuration.version, payload=payload, before=before, after=after)
        return after
