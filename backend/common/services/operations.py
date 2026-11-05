"""Shared authorization and idempotent receipts for shop operations."""
import json
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied
from rest_framework.renderers import JSONRenderer
from identity.services.access import accessible_shops, effective_permissions
from common.models import Activity
from common.services.errors import OperationConflict
from django.utils.translation import gettext_lazy as _


def json_value(value):
    # JSONRenderer treats a top-level None as an empty HTTP response. Here
    # None is a nullable operation fact, including before/after snapshots.
    if value is None:
        return None
    return json.loads(JSONRenderer().render(value))


def original_receipt(operation_id, shop, actor, action, request):
    existing = Activity.objects.filter(id=operation_id).first()
    if existing is None:
        from backups.models import RecoveryJournalOperation,RecoveryIntegrationDecision,RecoveryClosure
        if (RecoveryJournalOperation.objects.filter(operation_id=operation_id).exists()
                or RecoveryIntegrationDecision.objects.filter(operation_id=operation_id).exists()
                or RecoveryClosure.objects.filter(operation_id=operation_id).exists()):
            raise OperationConflict()
        return None
    if (existing.shop_id != shop.id or existing.actor_id != actor.id
            or existing.action != action or existing.details.get("request") != request):
        raise OperationConflict()
    return existing.details["response"]


def current_shop(shop_id, actor, installation):
    return get_object_or_404(accessible_shops(actor, installation), id=shop_id)


def require_manager(shop, actor):
    role, _permissions = effective_permissions(shop, actor)
    if role != "manager":
        raise PermissionDenied(_("Correction de fiche réservée au responsable."))
