"""Console-only classification while the exclusive normal gate drains all workers."""
import uuid
from django.db import transaction
from django.utils import timezone
from backups.models import HostingReminderAttempt, HostingReminder, HostingReminderRepair
from common.services.operations import json_value
from identity.models import User


class HostingRepairError(Exception):
    """A stable, non-sensitive console error code."""


@transaction.atomic
def _repair_under_control(actor,*,attempt_id,operation_id,reason,control):
    """Called only by the credential-checked console holding exclusive gate control.

    No SMTP is performed. The original actor, message, request and start time
    remain untouched; a separately attributed review records the classification.
    """
    if (control["mode"]!="open" or type(control["version"]) is not int or control["version"]<1):
        raise HostingRepairError("hosting_repair_gate_unavailable")
    if not User.objects.filter(id=actor.id,is_active=True,is_staff=True,is_superuser=True).exists():
        raise HostingRepairError("hosting_repair_forbidden")
    if not isinstance(reason,str) or not reason.strip() or len(reason)>2000:
        raise HostingRepairError("hosting_repair_input_invalid")
    reason=reason.strip()
    attempt_id,operation_id=uuid.UUID(str(attempt_id)),uuid.UUID(str(operation_id))
    payload=json_value({"attempt_id":attempt_id,"operation_id":operation_id,"actor_id":actor.id,
        "reason":reason,"acceptance_unknown_acknowledged":True})
    old=HostingReminderRepair.objects.filter(operation_id=operation_id).first()
    if old is not None:
        if old.payload!=payload:raise HostingRepairError("hosting_repair_conflict")
        return old.receipt
    attempt=HostingReminderAttempt.objects.select_related("reminder").filter(id=attempt_id).first()
    if (attempt is None or attempt.state!="sending" or attempt.finished_at is not None
            or attempt.reminder.state!="sending" or attempt.reminder.current_attempt_id!=attempt.id
            or attempt.reminder.version!=attempt.number
            or HostingReminderRepair.objects.filter(attempt=attempt).exists()):
        raise HostingRepairError("hosting_repair_conflict")
    now=timezone.now()
    if now<attempt.started_at:raise HostingRepairError("hosting_repair_clock_inconsistent")
    receipt=json_value({"operation_id":operation_id,"attempt_id":attempt.id,"reminder_id":attempt.reminder_id,
        "requested_by":actor.id,"reason":reason,"before_state":"sending","after_state":"uncertain",
        "failure_code":"smtp_worker_interrupted","recorded_at":now,"gate_mode":"open",
        "gate_version":control["version"],"smtp_sent":False})
    HostingReminderRepair.objects.create(attempt=attempt,operation_id=operation_id,actor=actor,
        gate_version=control["version"],reason=reason,payload=payload,receipt=receipt)
    attempt.state="uncertain";attempt.finished_at=now;attempt.failure_code="smtp_worker_interrupted"
    attempt.save(update_fields=["state","finished_at","failure_code","updated_at"])
    changed=HostingReminder.objects.filter(id=attempt.reminder_id,current_attempt=attempt,state="sending",version=attempt.number).update(
        state="uncertain",updated_at=now)
    if changed!=1:raise HostingRepairError("hosting_repair_conflict")
    return receipt
