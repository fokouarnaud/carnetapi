"""Explicit abandonment never erases a real SMTP result or deletes an archive."""
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError
from backups.models import BackupJob, BackupEvent
from backups.services.jobs import authorize
from backups.services.delivery import all_parts_accepted
from common.services.errors import OperationConflict


class BackupAbandoned(APIException):
    status_code=409
    default_code="backup_abandoned"
    default_detail={"code":"backup_abandoned"}


class BackupCannotBeAbandoned(APIException):
    status_code=409
    default_code="backup_cannot_be_abandoned"
    default_detail={"code":"backup_cannot_be_abandoned"}


def abandon_backup(actor,job_id,operation_id,reason,session=None):
    with transaction.atomic():
        authorize(actor,session)
        if not isinstance(reason,str) or not 1<=len(reason.strip())<=2000:
            raise ValidationError({"reason":"required"})
        reason=reason.strip()
        previous=BackupJob.objects.filter(abandon_operation_id=operation_id).first()
        if previous is not None:
            if previous.id!=job_id or previous.abandoned_by_id!=actor.id or previous.abandon_reason!=reason:
                raise OperationConflict()
            return previous,False
        job=get_object_or_404(BackupJob,id=job_id)
        if job.abandoned_at is not None:
            raise OperationConflict()
        if job.state not in {"ready","failed"} or job.cleanup_state!="waiting" or all_parts_accepted(job):
            raise BackupCannotBeAbandoned()
        job.abandon_operation_id,job.abandoned_at,job.abandoned_by,job.abandon_reason=operation_id,timezone.now(),actor,reason
        job.save(update_fields=["abandon_operation_id","abandoned_at","abandoned_by","abandon_reason","updated_at"])
        job.dispatches.filter(state="active").update(state="abandoned",finished_at=job.abandoned_at,lease_id=None,leased_until=None)
        BackupEvent.objects.create(job=job,kind="abandoned",details={"operation_id":str(operation_id),"actor":str(actor.id),"reason":reason})
        return job,True
