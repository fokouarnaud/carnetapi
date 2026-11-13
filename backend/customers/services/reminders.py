"""US-25 / Q83–Q88: WhatsApp reminders are prepared online from validated data and frozen.

Preparing through the API is the "successful synchronisation" Q85 requires: the
server state is current at that instant. Payments still offline on another
phone may be missing, so every preparation carries an explicit warning.
"""
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from common.models import Activity
from common.services.errors import OperationConflict
from customers.models import (CustomerDebt, CustomerLedgerEntry, CustomerPayment, CustomerReminder,
                              CustomerReminderDebt)
from identity.services.access import effective_permissions

MESSAGES = {
    "fr": "Bonjour {name}, petit rappel de {shop} : il reste {total} FCFA à régler. Merci.",
    "en": "Hello {name}, a reminder from {shop}: {total} FCFA remains to be paid. Thank you.",
}


class ReminderRefused(Exception):
    """A stable, non-localized refusal code mapped to an HTTP status by the API."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SelectedDebt:
    debt: CustomerDebt
    overdue: bool


def require_reminder_permission(shop, actor):
    role, permissions = effective_permissions(shop, actor)
    if role != "manager" and not permissions["can_prepare_reminder"]:
        raise PermissionDenied("Préparation de rappels non autorisée.")


def format_fcfa(amount, language):
    grouped = f"{amount:,}"
    return grouped.replace(",", " ") if language == "fr" else grouped


def is_blocked(customer):
    """Q86: a known payment awaiting validation or regularization blocks figure-bearing text."""
    return (CustomerLedgerEntry.objects.filter(customer=customer, state="pending").exists()
            or CustomerPayment.objects.filter(customer=customer, requires_regularization=True).exists())


def select_debts(customer, extra_debt_ids, now=None):
    """Q87: overdue debts by default; others only when explicitly added, never called overdue."""
    now = now or timezone.now()
    open_debts = CustomerDebt.objects.filter(customer=customer, remaining_fcfa__gt=0).order_by("occurred_at", "id")
    extras = set(extra_debt_ids)
    known = {debt.id for debt in open_debts.filter(id__in=extras)}
    if known != extras:
        raise ReminderRefused("reminder_debt_unknown")
    selected = []
    for debt in open_debts:
        overdue = debt.due_at is not None and debt.due_at <= now
        if overdue or debt.id in extras:
            selected.append(SelectedDebt(debt, overdue))
    if not selected:
        raise ReminderRefused("reminder_empty")
    return selected


def draft(shop, customer, extra_debt_ids, language):
    if is_blocked(customer):
        raise ReminderRefused("reminder_blocked_pending_payment")
    selected = select_debts(customer, extra_debt_ids)
    total = sum(row.debt.remaining_fcfa for row in selected)
    message = MESSAGES[language].format(name=customer.name, shop=shop.name, total=format_fcfa(total, language))
    return {
        "customer_id": str(customer.id),
        "language": language,
        "phone": customer.phone or None,
        "message": message,
        "total_fcfa": total,
        "debts": [{"debt_id": str(row.debt.id), "remaining_fcfa": row.debt.remaining_fcfa, "overdue": row.overdue}
                  for row in selected],
        "other_devices_warning": True,
    }


def receipt(reminder):
    return {
        "id": str(reminder.id),
        "customer_id": str(reminder.customer_id),
        "actor_id": str(reminder.actor_id),
        "language": reminder.language,
        "phone": reminder.phone,
        "message": reminder.message,
        "total_fcfa": reminder.total_fcfa,
        "debts": [{"debt_id": str(row.debt_id), "remaining_fcfa": row.remaining_fcfa, "overdue": row.overdue}
                  for row in reminder.debts.order_by("debt__occurred_at", "debt__id")],
        "status": reminder.status,
        "created_at": reminder.created_at.isoformat().replace("+00:00", "Z"),
        "declared_at": reminder.declared_at.isoformat().replace("+00:00", "Z") if reminder.declared_at else None,
        "declared_by": str(reminder.declared_by_id) if reminder.declared_by_id else None,
        "other_devices_warning": True,
    }


@transaction.atomic
def prepare_reminder(shop, customer, actor, installation, data):
    request = {"id": str(data["id"]), "customer_id": str(customer.id), "language": data["language"],
               "extra_debt_ids": sorted(str(value) for value in data["extra_debt_ids"])}
    existing = Activity.objects.filter(id=data["operation_id"]).first()
    if existing is not None:
        if (existing.action != "customer_reminder_prepared" or existing.shop_id != shop.id
                or existing.actor_id != actor.id or existing.details.get("request") != request):
            raise OperationConflict()
        return existing.details["response"], False
    if CustomerReminder.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    content = draft(shop, customer, data["extra_debt_ids"], data["language"])
    operation = Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor,
                                        action="customer_reminder_prepared", target_id=data["id"],
                                        details={"request": request})
    reminder = CustomerReminder.objects.create(
        id=data["id"], shop=shop, customer=customer, actor=actor, installation=installation, operation=operation,
        language=data["language"], phone=content["phone"], message=content["message"],
        total_fcfa=content["total_fcfa"])
    CustomerReminderDebt.objects.bulk_create([
        CustomerReminderDebt(reminder=reminder, debt_id=row["debt_id"], remaining_fcfa=row["remaining_fcfa"],
                             overdue=row["overdue"]) for row in content["debts"]])
    response = receipt(reminder)
    operation.details["response"] = response
    operation.save(update_fields=["details", "updated_at"])
    return response, True


@transaction.atomic
def declare_sent(reminder, actor, operation_id):
    """Q88: only a person's confirmation; a repeated declaration keeps the first one."""
    reminder = CustomerReminder.objects.select_for_update().get(pk=reminder.pk)
    if reminder.status == CustomerReminder.Status.SENT_DECLARED:
        return receipt(reminder)
    reminder.status = CustomerReminder.Status.SENT_DECLARED
    reminder.declared_at = timezone.now()
    reminder.declared_by = actor
    reminder.save(update_fields=["status", "declared_at", "declared_by", "updated_at"])
    Activity.objects.create(id=operation_id, shop=reminder.shop, actor=actor, action="customer_reminder_declared_sent",
                            target_id=reminder.id, details={"reminder_id": str(reminder.id)})
    return receipt(reminder)
