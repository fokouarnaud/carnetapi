from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import APIException

from common.services.operations import current_shop, json_value, original_receipt, require_manager
from customers.api.client_serializers import ClientSerializer
from common.services.errors import OperationConflict
from common.models import Activity
from customers.models import Customer
from customers.services.duplicate_queries import with_duplicate_hint
from identity.services.authentication import require_live_session


class ClientVersionConflict(APIException):
    status_code = 409
    default_detail = "La fiche a été modifiée. Relire sa version avant correction."
    default_code = "client_version_conflict"


def client_receipt(customer):
    current = with_duplicate_hint(Customer.objects.filter(pk=customer.pk)).get()
    return json_value(ClientSerializer(current).data)


@transaction.atomic
def create_client(shop_id, actor, installation, validated_data, session_id):
    require_live_session(actor, session_id, installation)
    shop = current_shop(shop_id, actor, installation)
    data = dict(validated_data)
    canonical = json_value(data)
    operation_id = data["operation_id"]
    original = original_receipt(operation_id, shop, actor, "client_created", canonical)
    if original is not None:
        return original, False
    if Customer.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    return _persist_client(shop=shop,actor=actor,installation=installation,data=data,canonical=canonical)


def _persist_client(*,shop,actor,installation,data,canonical,source_snapshot=None,origin_capture=None,recovery_review=None):
    """Persist validated creation under the caller's authority and transaction."""
    data=dict(data);operation_id=data.pop("operation_id")
    customer = Customer.objects.create(shop=shop, **data)
    if source_snapshot is not None:
        Customer.objects.filter(id=customer.id).update(created_at=source_snapshot["created_at"],updated_at=source_snapshot["updated_at"])
        customer.refresh_from_db()
    receipt = client_receipt(customer)
    event=Activity.objects.create(
        id=operation_id, shop=shop, actor=actor, action="client_created", target_id=customer.id,
        reason="Création de la fiche client", details={"request": canonical, "response": receipt,"installation_id":str(installation.id)},
    )
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=event,capture=origin_capture)
        event.details["recovery"]=json_value({"capture_id":origin_capture.id,"origin_user_id":origin_capture.origin_user_id,
            "origin_installation_id":origin_capture.origin_installation_id,"origin_recorded_at":origin_capture.origin_recorded_at,
            "origin_installation_verified":False,"source_snapshot":source_snapshot,"review":recovery_review})
        event.save(update_fields=["details","updated_at"])
    return receipt, True


@transaction.atomic
def update_client(shop_id, client_id, actor, installation, validated_data, session_id):
    require_live_session(actor, session_id, installation)
    shop = current_shop(shop_id, actor, installation)
    customer = get_object_or_404(Customer, shop=shop, id=client_id)
    require_manager(shop, actor)
    data = dict(validated_data)
    canonical = json_value({**data, "client_id": client_id})
    operation_id = data["operation_id"]
    original = original_receipt(operation_id, shop, actor, "client_updated", canonical)
    if original is not None:
        return original
    if customer.version != data["base_version"]:
        raise ClientVersionConflict()
    return _persist_client_update(shop=shop,customer=customer,actor=actor,installation=installation,data=data,canonical=canonical)


def _persist_client_update(*,shop,customer,actor,installation,data,canonical,source_snapshot=None,origin_capture=None,recovery_review=None):
    """Apply a verified versioned correction under the caller's transaction."""
    data=dict(data);operation_id=data.pop("operation_id")
    if customer.version!=data.pop("base_version"):raise ClientVersionConflict()
    reason = data.pop("reason")
    before = client_receipt(customer)
    for name, value in data.items():
        setattr(customer, name, value)
    customer.version += 1
    customer.save(update_fields=[*data, "version", "updated_at"])
    if source_snapshot is not None:
        Customer.objects.filter(id=customer.id).update(updated_at=source_snapshot["after"]["updated_at"])
        customer.refresh_from_db()
    receipt = client_receipt(customer)
    event=Activity.objects.create(
        id=operation_id, shop=shop, actor=actor, action="client_updated", target_id=customer.id, reason=reason,
        details={"request": canonical, "before": before, "after": receipt, "response": receipt,"installation_id":str(installation.id)},
    )
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=event,capture=origin_capture)
        event.details["recovery"]=json_value({"capture_id":origin_capture.id,"origin_user_id":origin_capture.origin_user_id,
            "origin_installation_id":origin_capture.origin_installation_id,"origin_recorded_at":origin_capture.origin_recorded_at,
            "origin_installation_verified":False,"source_snapshot":source_snapshot,"review":recovery_review})
        event.save(update_fields=["details","updated_at"])
    return receipt


@transaction.atomic
def report_correction(shop_id, client_id, actor, installation, validated_data, session_id):
    require_live_session(actor, session_id, installation)
    shop = current_shop(shop_id, actor, installation)
    customer = get_object_or_404(Customer, shop=shop, id=client_id)
    data = dict(validated_data)
    canonical = json_value({**data, "client_id": client_id})
    operation_id = data["operation_id"]
    original = original_receipt(operation_id, shop, actor, "client_correction_reported", canonical)
    if original is not None:
        return original, False
    activity = Activity.objects.create(
        id=operation_id, shop=shop, actor=actor, action="client_correction_reported",
        target_id=customer.id, reason=data["reason"], details={"request": canonical, "client_version": customer.version},
    )
    receipt = json_value({"operation_id": operation_id, "client_id": client_id, "reported_at": activity.created_at})
    activity.details["response"] = receipt
    activity.save(update_fields=["details", "updated_at"])
    return receipt, True
