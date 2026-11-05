"""Keep real collections even if an explicit allocation has become stale."""
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from identity.services.access import effective_permissions
from identity.services.authentication import require_live_session
from common.services.operations import current_shop, json_value, original_receipt
from common.services.errors import OperationConflict
from common.models import Activity
from cash.models import CashMovement
from customers.models import Customer, CustomerDebt, CustomerPayment, CustomerPaymentPart, DebtAllocation
from customers.api.payment_serializers import CustomerPaymentSerializer
from customers.services.ledger import append_customer_ledger
from customers.services.payment_references import possible_duplicate_reference


def payment_context(shop_id, customer_id, actor, installation, write=False):
    shop = current_shop(shop_id, actor, installation)
    customer = get_object_or_404(Customer, shop=shop, id=customer_id)
    _, permissions = effective_permissions(shop, actor)
    permission = "can_collect_debt" if write else "can_read_debt"
    if not permissions[permission]:
        raise PermissionDenied("Encaissement ou consultation non autorisé.")
    return shop, customer


def payment_preview(shop, customer, amount_fcfa):
    debts = CustomerDebt.objects.filter(shop=shop, customer=customer, remaining_fcfa__gt=0).order_by("occurred_at", "id")
    proposals = []
    known = 0
    remaining = amount_fcfa
    limit_reached = False
    for debt in debts.iterator(chunk_size=1000):
        known += debt.remaining_fcfa
        if remaining:
            if len(proposals) < 500:
                amount = min(remaining, debt.remaining_fcfa)
                proposals.append({"debt_id": debt.id, "amount_fcfa": amount})
                remaining -= amount
            else:
                limit_reached = True
    return {"amount_fcfa": amount_fcfa, "known_debt_fcfa": known, "unallocated_fcfa": remaining,
            "allocations": proposals, "allocation_limit_reached": limit_reached}


@transaction.atomic
def create_customer_payment(shop_id, customer_id, actor, installation, session_id, data):
    require_live_session(actor, session_id, installation)
    shop, customer = payment_context(shop_id, customer_id, actor, installation, write=True)
    canonical = json_value({**data, "customer_id": customer.id, "installation_id": installation.id})
    original = original_receipt(data["operation_id"], shop, actor, "customer_payment_created", canonical)
    if original is not None:
        return original, False
    from backups.models import RecoveryCollectionReview
    if RecoveryCollectionReview.objects.filter(payment_id=data["id"]).exists():raise OperationConflict()
    if (CustomerPayment.objects.filter(id=data["id"]).exists()
            or CustomerPaymentPart.objects.filter(id__in=[part["id"] for part in data["parts"]]).exists()
            or DebtAllocation.objects.filter(id__in=[row["id"] for row in data["allocations"]]).exists()):
        raise OperationConflict()
    return _persist_customer_payment(shop=shop,customer=customer,actor=actor,installation=installation,
        data=data,canonical=canonical,effective_at=timezone.now())


def _persist_customer_payment(*,shop,customer,actor,installation,data,canonical,effective_at,
        allocation_caps=None,origin_capture=None,recovery_review=None,source_snapshot=None):
    """Persist validated facts under the caller's current authority and transaction.

    Recovery caps are the frozen original applied amounts. They cannot turn an
    originally unallocated advance into a new automatic settlement.
    """
    amount = sum(part["amount_fcfa"] for part in data["parts"])
    now = effective_at
    audit = Activity.objects.create(
        id=data["operation_id"], shop=shop, actor=actor, action="customer_payment_created",
        target_id=data["id"], reason="Encaissement client", details={"request": canonical},
    )
    context={}
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=audit,capture=origin_capture)
        context["origin_identity"]={"actor_id":origin_capture.origin_user_id,"installation_id":origin_capture.origin_installation_id}
        audit.details["recovery"]=json_value({"capture_id":origin_capture.id,"origin_user_id":origin_capture.origin_user_id,
            "origin_installation_id":origin_capture.origin_installation_id,"origin_recorded_at":origin_capture.origin_recorded_at,
            "source_snapshot":source_snapshot,"review":recovery_review})
    payment = CustomerPayment.objects.create(
        id=data["id"], shop=shop, customer=customer, actor=actor, installation=installation,
        operation=audit, occurred_at=now, amount_fcfa=amount, unallocated_fcfa=amount,
    )
    applied_total = 0
    for position, request in enumerate(data["allocations"]):
        debt = get_object_or_404(CustomerDebt, shop=shop, customer=customer, id=request["debt_id"])
        cap=request["amount_fcfa"] if allocation_caps is None else allocation_caps[position]
        applied = min(debt.remaining_fcfa, cap)
        DebtAllocation.objects.create(
            id=request["id"], payment=payment, debt=debt, position=position,
            requested_amount_fcfa=request["amount_fcfa"], applied_amount_fcfa=applied,
        )
        debt.remaining_fcfa -= applied
        debt.save(update_fields=["remaining_fcfa", "updated_at"])
        applied_total += applied
    payment.unallocated_fcfa = amount - applied_total
    payment.requires_regularization = payment.unallocated_fcfa > 0
    payment.save(update_fields=["unallocated_fcfa", "requires_regularization", "updated_at"])
    for position, part_data in enumerate(data["parts"]):
        part = CustomerPaymentPart.objects.create(payment=payment, position=position,
                                                  possible_duplicate_reference=possible_duplicate_reference(shop, part_data), **part_data)
        CashMovement.objects.create(
            shop=shop, actor=actor, installation=installation, operation=audit, customer_payment_part=part,
            method=part.method, amount_fcfa=part.amount_fcfa, occurred_at=now, reason="Encaissement client",
        )
    receipt = json_value(CustomerPaymentSerializer(payment,context=context).data)
    audit.details["response"] = receipt
    audit.save(update_fields=["details", "updated_at"])
    append_customer_ledger(shop, customer, audit, payment.id, "collection", now, -amount,
                           debt_delta_fcfa=-applied_total, credit_delta_fcfa=payment.unallocated_fcfa)
    return receipt, True
