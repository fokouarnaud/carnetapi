"""Reported expiration and one SMTP action per tick; no host renewal API is called."""
from datetime import timedelta
from email.utils import parseaddr
from pathlib import Path
import shutil
import smtplib
import uuid
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.mail import EmailMessage, BadHeaderError
from django.core.validators import validate_email
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from backups.models import HostingConfiguration, HostingConfigurationChange, HostingReminder, HostingReminderAttempt, HostingObservation, RestoreAccessGeneration
from backups.services.jobs import authorize
from backups.services.health import backup_health
from backups.services.worker_gate import normal_database_work
from backups.services.restore_gate import RestoreGateError
from common.services.errors import OperationConflict
from common.services.operations import json_value
from identity.models import User

DOUALA=ZoneInfo("Africa/Douala")


class HostingUnavailable(APIException):
    status_code=503
    default_code="hosting_reminder_unavailable"
    default_detail={"code":"hosting_reminder_unavailable"}


def generation_id():
    return RestoreAccessGeneration.objects.filter(current_slot=1).values_list("id",flat=True).first()


def configuration():
    return HostingConfiguration.objects.filter(singleton=1).first()


def configuration_data(current=None):
    current=current if current is not None else configuration()
    return json_value({"id":current.id if current else None,"version":current.version if current else 0,
        "enabled":current.enabled if current else False,"expires_at":current.expires_at if current else None,
        "recipient":current.recipient if current else "","language":current.language if current else "fr",
        "evidence_reference":current.evidence_reference if current else "","timezone_name":"Africa/Douala",
        "updated_at":current.updated_at if current else None,"updated_by_id":current.updated_by_id if current else None,
        "verified_generation_id":current.verified_generation_id if current else None,
        "last_reported_renewal_at":current.last_reported_renewal_at if current else None,
        "renewal_source":"operator_report"})


@transaction.atomic
def change_hosting(actor,data,session=None):
    from backups.api.hosting import HostingConfigurationInput
    authorize(actor,session)
    incoming=HostingConfigurationInput(data=data);incoming.is_valid(raise_exception=True)
    values=incoming.validated_data
    request=json_value(values)
    previous=HostingConfigurationChange.objects.filter(operation_id=values["operation_id"]).first()
    if previous is not None:
        if previous.actor_id!=actor.id or previous.payload!=request:raise OperationConflict()
        return previous.after
    current=configuration()
    version=current.version if current else 0
    if version!=values["expected_version"]:raise OperationConflict()
    now=timezone.now()
    if values["action"]=="confirm_renewal" and (values["expires_at"]<=now or (current is not None and values["expires_at"]<=current.expires_at)):
        raise ValidationError({"code":"renewal_requires_later_future_expiration"})
    before=configuration_data(current)
    current=current or HostingConfiguration()
    current.version=version+1
    for name in ("enabled","expires_at","recipient","language","evidence_reference"):
        setattr(current,name,values[name])
    current.updated_by=actor
    current.verified_generation_id=generation_id()
    if values["action"]=="confirm_renewal":current.last_reported_renewal_at=now
    current.save()
    for days in (7,2):HostingReminder.objects.get_or_create(expires_at=current.expires_at,lead_days=days)
    after=configuration_data(current)
    HostingConfigurationChange.objects.create(configuration=current,operation_id=values["operation_id"],actor=actor,
        previous_version=version,version=current.version,action=values["action"],reason=values["reason"],
        payload=request,before=before,after=after)
    return after


def reminder_data(reminder,now):
    due_date=reminder.expires_at.astimezone(DOUALA).date()-timedelta(days=reminder.lead_days)
    effective_state=reminder.state
    if effective_state=="pending" and now.astimezone(DOUALA).date()>due_date:effective_state="missed"
    return json_value({"id":reminder.id,"expires_at":reminder.expires_at,"lead_days":reminder.lead_days,
        "due_local_date":due_date,"state":reminder.state,"effective_state":effective_state,
        "version":reminder.version,"current_attempt_id":reminder.current_attempt_id})


def attempt_data(attempt):
    return json_value({"id":attempt.id,"reminder_id":attempt.reminder_id,"number":attempt.number,
        "command_id":attempt.command_id,"actor_id":attempt.actor_id,"configuration_version":attempt.configuration_version,
        "recipient":attempt.recipient,"language":attempt.language,"message_id":attempt.message_id,
        "state":attempt.state,"started_at":attempt.started_at,"finished_at":attempt.finished_at,
        "failure_code":attempt.failure_code,"reason":attempt.reason})


def _worker_actor():
    try:identity=uuid.UUID(str(getattr(settings,"HOSTING_REMINDER_ACTOR_ID","")))
    except (ValueError,TypeError,AttributeError):raise HostingUnavailable() from None
    actor=User.objects.filter(id=identity,is_active=True,is_superuser=True).first()
    if actor is None:raise HostingUnavailable()
    return actor


def _message(current,reminder):
    expires=current.expires_at.astimezone(DOUALA).isoformat()
    if current.language=="fr":
        return (f"Hébergement : expiration déclarée dans {reminder.lead_days} jours",
            f"Expiration déclarée : {expires} (Africa/Douala).\n"
            "Vérifiez cette date dans la console de l'hébergeur et effectuez le renouvellement manuel.\n"
            "Consignez ensuite la nouvelle date et sa référence de vérification dans l'administration.\n"
            "Une visite, un rechargement ou ce rappel ne prolonge pas l'hébergement.\n")
    return (f"Hosting: reported expiry in {reminder.lead_days} days",
        f"Reported expiry: {expires} (Africa/Douala).\n"
        "Verify the date in the hosting console and renew manually.\n"
        "Then record the new expiry and its verification reference in administration.\n"
        "A visit, reload or this reminder does not extend hosting.\n")


def _ready(current,now):
    if current is None:return "unconfigured"
    if not current.enabled:return "disabled"
    if now<current.updated_at:return "clock_inconsistent"
    if current.verified_generation_id!=generation_id():return "awaiting_verification"
    return None


def _claim(actor,current,reminder,now,*,command_id=None,request=None,reason=""):
    # Caller holds SQLite's write reservation and rechecked current authority.
    sender=parseaddr(settings.DEFAULT_FROM_EMAIL)[1]
    try:validate_email(sender)
    except DjangoValidationError:raise HostingUnavailable() from None
    subject,body=_message(current,reminder)
    message_id=(reminder.current_attempt.message_id if reminder.current_attempt_id is not None
        else f"<hosting-{reminder.id.hex}@{sender.rsplit('@',1)[1]}>")
    attempt=HostingReminderAttempt.objects.create(reminder=reminder,number=reminder.version+1,actor=actor,
        command_id=command_id,configuration_version=current.version,recipient=current.recipient,sender=sender,
        language=current.language,message_id=message_id,
        subject=subject,body=body,reason=reason,request=request or {},started_at=now)
    reminder.version=attempt.number
    reminder.state="sending"
    reminder.current_attempt=attempt
    reminder.save(update_fields=["version","state","current_attempt","updated_at"])
    return attempt


def _deliver(attempt):
    # Network I/O is outside the claim transaction, inside the maintenance lease.
    authorize(attempt.actor)
    message=EmailMessage(attempt.subject,attempt.body,attempt.sender,[attempt.recipient],
        headers={"Message-ID":attempt.message_id})
    failure=""
    try:
        accepted=message.send(fail_silently=False)
        state="accepted" if accepted==1 else "failed"
        if state=="failed":failure="smtp_not_accepted"
    except (smtplib.SMTPAuthenticationError,smtplib.SMTPRecipientsRefused,smtplib.SMTPSenderRefused,
            smtplib.SMTPConnectError,smtplib.SMTPHeloError,smtplib.SMTPDataError,ConnectionRefusedError,BadHeaderError):
        state="failed";failure="smtp_rejected"
    except (smtplib.SMTPException,OSError,UnicodeError):
        state="uncertain";failure="smtp_acceptance_uncertain"
    with transaction.atomic():
        current=HostingReminderAttempt.objects.get(id=attempt.id)
        if current.state!="sending":raise OperationConflict()
        current.state=state;current.finished_at=timezone.now();current.failure_code=failure
        current.save(update_fields=["state","finished_at","failure_code","updated_at"])
        if not HostingReminder.objects.filter(id=current.reminder_id,current_attempt=current,state="sending").update(state=state,updated_at=timezone.now()):
            raise OperationConflict()
        return attempt_data(current)


def _automatic_tick():
    with transaction.atomic():
        actor=_worker_actor()
        now=timezone.now();current=configuration()
        unavailable=_ready(current,now)
        if unavailable:return unavailable
        delta=(current.expires_at.astimezone(DOUALA).date()-now.astimezone(DOUALA).date()).days
        if delta not in (7,2):return "idle"
        reminder=HostingReminder.objects.get(expires_at=current.expires_at,lead_days=delta)
        if reminder.state!="pending":return "busy" if reminder.state=="sending" else "idle"
        attempt=_claim(actor,current,reminder,now)
    return _deliver(attempt)["state"]


def hosting_tick():
    try:
        with normal_database_work():return _automatic_tick()
    except RestoreGateError:raise HostingUnavailable() from None


def retry_hosting(actor,identity,data,session=None):
    from backups.api.hosting import HostingRetryInput
    try:
        with normal_database_work():
            with transaction.atomic():
                authorize(actor,session)
                incoming=HostingRetryInput(data=data);incoming.is_valid(raise_exception=True)
                values=incoming.validated_data;request=json_value({**values,"reminder_id":identity})
                old=HostingReminderAttempt.objects.filter(command_id=values["operation_id"]).first()
                if old is not None:
                    if old.actor_id!=actor.id or old.request!=request:raise OperationConflict()
                    return attempt_data(old),False
                current=configuration();now=timezone.now()
                if _ready(current,now):raise OperationConflict()
                reminder=get_object_or_404(HostingReminder,id=identity,expires_at=current.expires_at)
                if (reminder.state not in ("failed","uncertain") or reminder.current_attempt_id!=values["expected_attempt_id"]
                        or not values["possible_duplicate_confirmed"]):raise OperationConflict()
                attempt=_claim(actor,current,reminder,now,command_id=values["operation_id"],request=request,reason=values["reason"])
            return _deliver(attempt),True
    except RestoreGateError:raise HostingUnavailable() from None


@transaction.atomic
def record_hosting_observation(actor,data,session=None):
    from backups.api.hosting import HostingObservationInput
    authorize(actor,session)
    incoming=HostingObservationInput(data=data);incoming.is_valid(raise_exception=True)
    values=incoming.validated_data;request=json_value(values)
    old=HostingObservation.objects.filter(operation_id=values["operation_id"]).first()
    if old is not None:
        if old.actor_id!=actor.id or old.request!=request:raise OperationConflict()
        return old.receipt,False
    if HostingObservation.objects.filter(id=values["id"]).exists():raise OperationConflict()
    row=HostingObservation.objects.create(actor=actor,request=request,receipt={},**values)
    receipt=json_value({"id":row.id,"operation_id":row.operation_id,"actor_id":actor.id,"observed_at":row.observed_at,
        "recorded_at":row.created_at,**{name:getattr(row,name) for name in ("installed_bytes","temporary_peak_bytes","quota_used_bytes",
            "quota_limit_bytes","service_state","evidence_reference","reason")}})
    row.receipt=receipt;row.save(update_fields=["receipt","updated_at"])
    return receipt,True


def reported_space(row):
    if row is None:return None
    return json_value({"id":row.id,"observed_at":row.observed_at,"recorded_at":row.created_at,
        "installed_bytes":None if row.installed_bytes is None else str(row.installed_bytes),
        "temporary_peak_bytes":None if row.temporary_peak_bytes is None else str(row.temporary_peak_bytes),
        "quota_used_bytes":None if row.quota_used_bytes is None else str(row.quota_used_bytes),
        "quota_limit_bytes":None if row.quota_limit_bytes is None else str(row.quota_limit_bytes),
        "quota_remaining_bytes":None if row.quota_used_bytes is None or row.quota_limit_bytes is None else str(row.quota_limit_bytes-row.quota_used_bytes),
        "installed_budget_state":"unknown" if row.installed_bytes is None else "reported_below_limit" if row.installed_bytes<500000000 else "reported_limit_exceeded",
        "service_state":row.service_state,"evidence_reference":row.evidence_reference})


def filesystem_space():
    name=str(settings.DATABASES["default"]["NAME"])
    if name==":memory:" or name.startswith("file:memory"):return None
    try:return str(shutil.disk_usage(Path(name).resolve().parent).free)
    except OSError:return None


@transaction.atomic
def hosting_status(actor,session=None):
    authorize(actor,session)
    now=timezone.now();current=configuration()
    ready=_ready(current,now)
    state="unknown" if current is None else "expired" if current.expires_at<=now else "expiring" if (current.expires_at.astimezone(DOUALA).date()-now.astimezone(DOUALA).date()).days<=7 else "active"
    reminders=list(HostingReminder.objects.filter(expires_at=current.expires_at).order_by("-lead_days")) if current else []
    summaries=[reminder_data(row,now) for row in reminders]
    observation=HostingObservation.objects.order_by("-observed_at","-id").first()
    reported=reported_space(observation)
    health=backup_health(actor,at=now,session=session)
    return json_value({"checked_at":now,"expiration_state":state,"configuration_state":ready or "verified",
        "configuration":configuration_data(current),"reminders":summaries,"reported_space":reported,
        "filesystem_free_bytes":filesystem_space(),"filesystem_measurement_scope":"database_filesystem_not_provider_quota",
        "backup":{"blocked":health["blocked"],"blocked_count":health["blocked_count"],"last_accepted_at":health["last_accepted_at"],
            "last_accepted_age_seconds":health["last_accepted_age_seconds"],"clock_inconsistent":health["clock_inconsistent"]},
        "requires_attention":state in ("unknown","expired","expiring") or ready in ("clock_inconsistent","awaiting_verification")
            or any(row["effective_state"] in ("failed","uncertain","sending","missed") for row in summaries) or health["blocked"]
            or (reported is not None and (reported["installed_budget_state"]=="reported_limit_exceeded" or reported["service_state"]=="down"
                or (reported["quota_remaining_bytes"] is not None and int(reported["quota_remaining_bytes"])<=0)))})
