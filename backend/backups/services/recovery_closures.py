"""Freeze partial technical recovery without declaring lost operations recovered."""
import hashlib

from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from backups.journal_protocol import canonical_bytes
from backups.models import (RecoveryClosure, RestoreSession, RestoreAccessGeneration,
    RecoveryJournalOperation, RecoveryIntegrationDecision, RecoverySaleHold, RecoveryCollectionReview)
from backups.services.jobs import authorize
from common.api.fields import StrictSerializer
from common.models import Activity
from common.services.errors import OperationConflict
from common.services.operations import json_value


class RecoveryClosureInput(StrictSerializer):
    operation_id=serializers.UUIDField()
    restore_session_id=serializers.UUIDField()
    kind=serializers.ChoiceField(choices=["partial","complete"])
    preview_token=serializers.RegexField(r"^[0-9a-f]{64}$")
    reason=serializers.CharField(max_length=2000,allow_blank=False)
    missing_device_ids=serializers.ListField(child=serializers.UUIDField(),max_length=1000)

    def validate_missing_device_ids(self,values):
        if len(set(values))!=len(values):
            raise serializers.ValidationError(_("Duplicate missing recovery devices."))
        return sorted(values,key=str)


def _registry(identifier):
    registry=RestoreSession.objects.select_related("generation").filter(id=identifier,generation__current_slot=1).first()
    if registry is None:raise OperationConflict()
    return registry


def _observed(queryset):
    return queryset.aggregate(count=Count("id"),latest_modified_at=Max("updated_at"))


def _reviews(queryset):
    return queryset.aggregate(count=Count("id"),pending=Count("id",filter=Q(state="pending")),
        refused=Count("id",filter=Q(state="refused")),validated=Count("id",filter=Q(state="validated")),
        latest_modified_at=Max("updated_at"))


def _snapshot(registry):
    observations=list(registry.devices.order_by("id").values("id","shop_id","origin_installation_id","origin_user_id"))
    captures=RecoveryJournalOperation.objects.filter(session=registry)
    scoped_counts={str(row['shop_id']):row['count'] for row in captures.values('shop_id').annotate(
        count=Count('id')).order_by('shop_id')}
    return json_value({"session_id":registry.id,"generation_id":registry.generation_id,
        "snapshot_at":registry.snapshot_at,"inventory_recorded_at":registry.inventory_recorded_at,
        "known_devices":len(observations) if registry.inventory_recorded_at is not None else None,
        "device_observations":observations,
        "captured_operations":_observed(captures),"captured_operation_counts":scoped_counts,
        "integration_decisions":_observed(RecoveryIntegrationDecision.objects.filter(capture__session=registry)),
        "sale_reviews":_reviews(RecoverySaleHold.objects.filter(capture__session=registry)),
        "collection_reviews":_reviews(RecoveryCollectionReview.objects.filter(capture__session=registry)),
        "closure_id":RecoveryClosure.objects.filter(session=registry).values_list("id",flat=True).first()})


@transaction.atomic
def preview_recovery_closure(*,actor,restore_session_id,session=None):
    """Show observed data to a current technical operator; counts prove no completeness."""
    authorize(actor,session)
    registry=_registry(restore_session_id)
    snapshot=_snapshot(registry)
    return {"snapshot":snapshot,"preview_token":hashlib.sha256(canonical_bytes(snapshot)).hexdigest(),
        "can_close_partial":snapshot["closure_id"] is None and registry.inventory_recorded_at is not None
            and bool(snapshot["device_observations"]),
        "can_close_complete":False,"exhaustive_recovery_guaranteed":False}


@transaction.atomic
def close_recovery(*,actor,data,session=None):
    """Record one partial declaration atomically, preserving its exact original receipt."""
    authorize(actor,session)
    # Reserve SQLite's writer before checking the current generation and preview.
    if not RestoreAccessGeneration.objects.filter(current_slot=1).update(current_slot=1):raise OperationConflict()
    authorize(actor,session)
    incoming=RecoveryClosureInput(data=data);incoming.is_valid(raise_exception=True)
    request=json_value(incoming.validated_data)
    registry=_registry(request["restore_session_id"])
    installation_id=session.installation_id if session is not None else None
    previous=RecoveryClosure.objects.filter(operation_id=request["operation_id"]).first()
    if previous is not None:
        if (previous.session_id!=registry.id or previous.closed_by_id!=actor.id
                or previous.closed_installation_id!=installation_id or previous.request!=request):raise OperationConflict()
        return previous.receipt,False
    if request["kind"]=="complete":
        raise ValidationError({"code":"recovery_complete_closure_unproven"},code="recovery_complete_closure_unproven")
    if registry.inventory_recorded_at is None or not request["missing_device_ids"]:
        raise ValidationError({"code":"recovery_missing_device_required"},code="recovery_missing_device_required")
    if (Activity.objects.filter(id=request["operation_id"]).exists()
            or RecoveryJournalOperation.objects.filter(operation_id=request["operation_id"]).exists()
            or RecoveryIntegrationDecision.objects.filter(operation_id=request["operation_id"]).exists()
            or RecoveryClosure.objects.filter(session=registry).exists()):raise OperationConflict()
    snapshot=_snapshot(registry)
    observations={item["id"]:item for item in snapshot["device_observations"]}
    if not set(request["missing_device_ids"]).issubset(observations):
        raise ValidationError({"code":"recovery_missing_device_invalid"},code="recovery_missing_device_invalid")
    if request["preview_token"]!=hashlib.sha256(canonical_bytes(snapshot)).hexdigest():raise OperationConflict()
    missing=[observations[identifier] for identifier in request["missing_device_ids"]]
    closure=RecoveryClosure.objects.create(session=registry,operation_id=request["operation_id"],closed_by=actor,
        closed_installation_id=installation_id,kind="partial",reason=request["reason"],request=request,snapshot=snapshot,
        missing_devices=missing,receipt={})
    receipt=json_value({"id":closure.id,"operation_id":closure.operation_id,"restore_session_id":registry.id,
        "generation_id":registry.generation_id,"state":"closed_partial","kind":"partial","closed_by":actor.id,
        "closed_installation_id":installation_id,"closed_at":closure.created_at,"reason":closure.reason,
        "missing_devices":missing,"snapshot":snapshot,"exhaustive_recovery_guaranteed":False})
    closure.receipt=receipt;closure.save(update_fields=["receipt","updated_at"])
    return receipt,True


def require_recovery_integration_open(capture):
    """Hold new effects after closure until the required reopening/reconciliation is defined.

    Writers call this after recognizing an exact committed decision's receipt.
    Capturing a surviving journal remains allowed and never creates an effect.
    """
    if RecoveryClosure.objects.filter(session_id=capture.session_id).exists():raise OperationConflict()
