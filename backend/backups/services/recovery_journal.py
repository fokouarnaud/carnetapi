"""Capture a journal atomically without applying its business operations."""
import hashlib

from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import PermissionDenied,ValidationError

from backups.journal_protocol import JournalDepositInput,MAX_JOURNAL_BYTES,canonical_bytes
from backups.models import RestoreAccessGeneration,RestoreSession,RecoveryJournalDeposit,RecoveryJournalOperation
from common.services.operations import current_shop,require_manager,json_value
from common.services.errors import OperationConflict
from identity.services.authentication import require_live_session


class JournalAccessDenied(PermissionDenied):
    default_detail=_("Only a current shop responsible can deposit or inspect recovery journals.")


def journal_context(shop_id,actor,session_id):
    session=require_live_session(actor,session_id)
    shop=current_shop(shop_id,actor,session.installation)
    try:require_manager(shop,actor)
    except PermissionDenied:raise JournalAccessDenied() from None
    return shop,session


@transaction.atomic
def capture_recovery_journal(*,shop_id,actor,session_id,data):
    shop,live=journal_context(shop_id,actor,session_id)
    # Acquire SQLite's write reservation before rechecking roles and generations.
    if not RestoreAccessGeneration.objects.filter(current_slot=1).update(current_slot=1):raise OperationConflict()
    shop,live=journal_context(shop_id,actor,session_id)
    serializer=JournalDepositInput(data=data)
    serializer.is_valid(raise_exception=True)
    body=json_value(serializer.validated_data)
    encoded=canonical_bytes(body)
    if len(encoded)>MAX_JOURNAL_BYTES:
        raise ValidationError(_("Invalid or oversized recovery journal."),code="recovery_journal_invalid")
    if body["shop_id"]!=str(shop.id):
        raise ValidationError(_("Recovery journal shop does not match its destination."),code="recovery_journal_shop_invalid")
    registry=RestoreSession.objects.filter(generation__current_slot=1).first()
    if registry is None or str(registry.id)!=body["session_id"]:raise OperationConflict()
    digest=hashlib.sha256(encoded).hexdigest()
    previous=RecoveryJournalDeposit.objects.filter(operation_id=body["operation_id"]).first()
    if previous is not None:
        if (previous.session_id!=registry.id or previous.shop_id!=shop.id or previous.submitted_by_id!=actor.id
                or previous.submitted_installation_id!=live.installation_id or previous.content_sha256!=digest):raise OperationConflict()
        return previous.receipt,False
    existing={str(row.operation_id):row for row in RecoveryJournalOperation.objects.filter(
        session=registry,operation_id__in=[entry["operation_id"] for entry in body["operations"]])}
    prepared=[]
    for entry in body["operations"]:
        content=hashlib.sha256(canonical_bytes({"shop_id":str(shop.id),"format_version":1,**entry})).hexdigest()
        old=existing.get(entry["operation_id"])
        if old is not None and (old.shop_id!=shop.id or old.content_sha256!=content):raise OperationConflict()
        prepared.append((entry,old,content))
    deposit=RecoveryJournalDeposit.objects.create(session=registry,shop=shop,operation_id=body["operation_id"],
        submitted_by=actor,submitted_installation=live.installation,reason=body["reason"],format_version=1,
        content_sha256=digest,receipt={})
    fresh=[]
    captures=[]
    for entry,old,content in prepared:
        capture=old or RecoveryJournalOperation(session=registry,shop=shop,first_deposit=deposit,
            **entry,content_sha256=content)
        if old is None:fresh.append(capture)
        captures.append({"operation_id":str(capture.operation_id),"capture_id":str(capture.id)})
    RecoveryJournalOperation.objects.bulk_create(fresh)
    receipt=json_value({"id":deposit.id,"operation_id":deposit.operation_id,"session_id":registry.id,"shop_id":shop.id,
        "submitted_by":actor.id,"submitted_installation_id":live.installation_id,"reason":deposit.reason,
        "received_at":deposit.created_at,"format_version":1,"capture_state":"captured",
        "operation_count":len(captures),"new_operation_count":len(fresh),"business_effects_created":0,"operations":captures})
    deposit.receipt=receipt
    deposit.save(update_fields=["receipt","updated_at"])
    return receipt,True
