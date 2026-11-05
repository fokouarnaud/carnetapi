from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import APIException

from identity.services.authentication import require_live_session
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from common.services.dates import due_at_utc
from common.services.errors import OperationConflict
from common.models import Activity
from customers.models import Customer, CustomerDebt
from customers.services.ledger import append_customer_ledger


class DebtDueVersionConflict(APIException):
    status_code = 409
    default_code = "debt_due_version_conflict"
    default_detail = "L'échéance a changé. Relire avant de confirmer."


def manager_debt_context(shop_id, client_id, actor, installation):
    shop = current_shop(shop_id, actor, installation)
    require_manager(shop, actor)
    return shop, get_object_or_404(Customer, shop=shop, id=client_id)


@transaction.atomic
def open_debt(shop_id, client_id, actor, installation, session_id, data):
    require_live_session(actor, session_id, installation)
    shop, customer = manager_debt_context(shop_id, client_id, actor, installation)
    canonical = json_value({**data, "customer_id": customer.id, "installation_id": installation.id})
    original = original_receipt(data["operation_id"], shop, actor, "customer_debt_opened", canonical)
    if original is not None:
        return original, False
    if CustomerDebt.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    deadline = due_at_utc(data["due_date"], shop.timezone)
    return _persist_open_debt(shop=shop,customer=customer,actor=actor,installation=installation,data=data,canonical=canonical,deadline=deadline)


def _persist_open_debt(*,shop,customer,actor,installation,data,canonical,deadline,origin_capture=None,recovery_review=None,source_snapshot=None):
    """Append a verified opening without sale or cash under the caller's transaction."""
    from customers.api.debts import CustomerDebtSerializer
    event = Activity.objects.create(
        id=data["operation_id"], shop=shop, actor=actor, action="customer_debt_opened", target_id=data["id"],
        reason=data["reason"], details={"request": canonical},
    )
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=event,capture=origin_capture)
        event.details["recovery"]=json_value({"capture_id":origin_capture.id,"origin_user_id":origin_capture.origin_user_id,
            "origin_installation_id":origin_capture.origin_installation_id,"origin_recorded_at":origin_capture.origin_recorded_at,
            "origin_installation_verified":False,"source_snapshot":source_snapshot,"review":recovery_review})
    debt = CustomerDebt.objects.create(
        id=data["id"], shop=shop, customer=customer, actor=actor, installation=installation, operation=event,
        amount_fcfa=data["amount_fcfa"], remaining_fcfa=data["amount_fcfa"], occurred_at=data["occurred_at"], due_at=deadline,
    )
    append_customer_ledger(shop, customer, event, debt.id, "opening_debt", debt.occurred_at,
                           debt.amount_fcfa, debt_delta_fcfa=debt.amount_fcfa)
    receipt = json_value(CustomerDebtSerializer(debt).data)
    event.details["response"] = receipt
    event.save(update_fields=["details", "updated_at"])
    return receipt, True


@transaction.atomic
def update_debt_due_date(shop_id, client_id, debt_id, actor, installation, session_id, data):
    require_live_session(actor, session_id, installation)
    shop, customer = manager_debt_context(shop_id, client_id, actor, installation)
    debt = get_object_or_404(CustomerDebt, shop=shop, customer=customer, id=debt_id)
    canonical = json_value({**data, "debt_id": debt.id, "installation_id": installation.id})
    original = original_receipt(data["operation_id"], shop, actor, "customer_debt_due_changed", canonical)
    if original is not None:
        return original
    if debt.due_version != data["base_version"]:
        raise DebtDueVersionConflict()
    return _persist_debt_due_change(shop=shop,customer=customer,debt=debt,actor=actor,installation=installation,data=data,
        canonical=canonical,deadline=due_at_utc(data["due_date"],shop.timezone))


def _persist_debt_due_change(*,shop,customer,debt,actor,installation,data,canonical,deadline,effective_at=None,
        source_snapshot=None,origin_capture=None,recovery_review=None):
    """Change only the verified deadline/version, preserving every money field."""
    from customers.api.debts import CustomerDebtSerializer
    if debt.due_version!=data["base_version"]:raise DebtDueVersionConflict()
    before_due_at = json_value(CustomerDebtSerializer(debt).data)["due_at"]
    debt.due_at = deadline
    debt.due_version += 1
    debt.save(update_fields=["due_at", "due_version", "updated_at"])
    receipt = json_value(CustomerDebtSerializer(debt).data)
    event = Activity.objects.create(
        id=data["operation_id"], shop=shop, actor=actor, action="customer_debt_due_changed", target_id=debt.id,
        reason=data["reason"], details={"request": canonical, "before_due_at": before_due_at,
                                      "after_due_at": receipt["due_at"], "response": receipt},
    )
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=event,capture=origin_capture)
        event.details["recovery"]=json_value({"capture_id":origin_capture.id,"origin_user_id":origin_capture.origin_user_id,
            "origin_installation_id":origin_capture.origin_installation_id,"origin_recorded_at":origin_capture.origin_recorded_at,
            "origin_installation_verified":False,"source_snapshot":source_snapshot,"review":recovery_review})
        event.save(update_fields=["details","updated_at"])
    append_customer_ledger(shop, customer, event, event.id, "due_date_changed", effective_at or event.created_at, 0)
    return receipt
