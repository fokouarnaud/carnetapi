"""Commit a fully paid sale and all its physical and financial effects together."""
from stock.services.stock_alert_services import observe_stock
import uuid

from django.db import transaction
from django.db.models import Q
from stock.services.stock_queries import StockTotal
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from identity.services.access import effective_permissions
from identity.services.authentication import require_live_session
from catalogue.services.catalogue_services import ProductVersionConflict
from common.services.operations import current_shop, json_value, original_receipt
from common.services.errors import OperationConflict
from common.services.dates import due_at_utc
from common.models import Activity
from cash.models import CashMovement
from common.services.backdating import require_allowed_backdate
from customers.services.merges import resolve_customer
from customers.models import Customer, CustomerCredit, CustomerDebt
from catalogue.models import Product, ProductPack
from sales.models import Sale, SaleLine, SalePayment
from stock.models import StockMovement
from customers.services.payment_references import possible_duplicate_reference
from catalogue.services.pack_services import ArchivedProductConflict
from sales.services.pricing import LineAmount, MAX_FCFA, PricingFailure, line_reference_amount, price_basket
from sales.api.sale_serializers import SaleSerializer
from customers.services.ledger import append_customer_ledger
from stock.services.movement_costs import freeze_sale_costs


def require_pricing_result(result):
    if isinstance(result, PricingFailure):
        raise ValidationError({"code": result.code, "line_ids": [str(value) for value in result.line_ids]})
    return result


class DebtConfirmationRequired(APIException):
    status_code = 409
    default_code = "debt_confirmation_required"

    def __init__(self, known_debt_fcfa):
        super().__init__("Confirmer la dette connue actuelle avant cette vente.")
        self.detail = {"code": self.default_code, "known_debt_fcfa": known_debt_fcfa}


@transaction.atomic
def create_sale(shop_id, actor, installation, validated_data, session_id):
    # Authentication precedes BEGIN; re-check revocations under the write lock.
    require_live_session(actor, session_id, installation)
    shop = current_shop(shop_id, actor, installation)
    data = dict(validated_data)
    role, permissions = effective_permissions(shop, actor)
    if "occurred_at" in data and role != "manager":
        raise PermissionDenied(gettext_lazy("Vente datée manuellement réservée au responsable."))
    if data["mode"] == "credit" and not permissions["can_grant_credit"]:
        raise PermissionDenied("Vente à crédit non autorisée.")
    canonical = json_value({**data, "installation_id": installation.id})
    receipt = original_receipt(data["operation_id"], shop, actor, "sale_created", canonical)
    if receipt is not None:
        return receipt, False
    if "occurred_at" in data:
        require_allowed_backdate(shop, actor, data["occurred_at"], data.get("backdate_reason"))
    from backups.models import RecoverySaleHold
    if RecoverySaleHold.objects.filter(Q(source_operation_id=data["operation_id"])|Q(sale_id=data["id"])).exists():
        raise OperationConflict()
    customer = get_object_or_404(Customer, shop=shop, id=data["customer_id"]) if data["customer_id"] else None
    if data["mode"] == "credit" and customer is None:
        raise ValidationError({"customer_id": "Client obligatoire pour une vente à crédit."})
    # G18: the sale keeps the customer it was recorded for; receivables follow a merge redirection.
    account = resolve_customer(customer) if customer is not None else None
    if data["mode"] == "credit":
        known = sum(CustomerDebt.objects.filter(shop=shop, customer=account).values_list("remaining_fcfa", flat=True))
        if known and data["confirmed_debt_fcfa"] != known:
            raise DebtConfirmationRequired(known)
    due_at = due_at_utc(data["due_date"], shop.timezone)
    if (Sale.objects.filter(id=data["id"]).exists()
            or SaleLine.objects.filter(id__in=[line["id"] for line in data["lines"]]).exists()
            or SalePayment.objects.filter(id__in=[payment["id"] for payment in data["payments"]]).exists()):
        raise OperationConflict()
    snapshots = []
    amounts = []
    for line in data["lines"]:
        product = get_object_or_404(Product, id=line["product_id"], shop=shop)
        if not product.is_active:
            raise ArchivedProductConflict()
        if product.version != line["product_version"]:
            raise ProductVersionConflict()
        pack = get_object_or_404(ProductPack, id=line["pack_id"], product=product, is_active=True) if line["pack_id"] else None
        factor = pack.base_quantity_milli if pack else 1000
        base_quantity, remainder = divmod(line["quantity_milli"] * factor, 1000)
        if remainder or base_quantity > MAX_FCFA or (product.unit_kind == "piece" and base_quantity % 1000):
            raise ValidationError({"quantity_milli": "Quantité de stock non représentable exactement."})
        tariff = pack.sale_price_fcfa if pack else product.sale_price_fcfa
        reference = require_pricing_result(line_reference_amount(line["quantity_milli"], tariff, product.unit_kind))
        amounts.append(LineAmount(line["id"], reference.amount_fcfa, line["own_discount_fcfa"]))
        snapshots.append({
            "id": line["id"], "product": product, "product_version": product.version, "pack": pack,
            "product_name": product.name, "unit_kind": product.unit_kind,
            "unit_label": pack.name if pack else product.unit_label,
            "quantity_milli": line["quantity_milli"], "factor_milli": factor,
            "base_quantity_milli": base_quantity, "unit_price_fcfa": tariff,
        })
    cap = shop.seller_discount_cap_basis_points if role == "seller" else None
    basket = require_pricing_result(price_basket(amounts, data["global_discount_fcfa"], cap))
    paid = sum(payment["amount_fcfa"] for payment in data["payments"])
    if paid > MAX_FCFA or (data["mode"] == "cash" and paid < basket.net_total_fcfa):
        raise ValidationError({"payments": "Montant hors capacité ou paiement comptant inférieur au montant net."})
    excess = max(0, paid - basket.net_total_fcfa)
    if excess and customer is None:
        raise ValidationError({"customer_id": "Client obligatoire pour l'excédent ou la monnaie due."})
    quantities = {}
    for snapshot in snapshots:
        product_id = snapshot["product"].id
        quantities[product_id] = quantities.get(product_id, 0) + snapshot["base_quantity_milli"]
        if quantities[product_id] > MAX_FCFA:
            raise ValidationError({"quantity_milli": "Total de stock hors capacité."})
    stocks = {
        row["product_id"]: int(row["total"])
        for row in StockMovement.objects.filter(product_id__in=quantities).values("product_id").annotate(total=StockTotal("quantity_milli"))
    }
    remaining = [stocks.get(product_id, 0) - quantity for product_id, quantity in quantities.items()]
    if any(value < -MAX_FCFA - 1 for value in remaining):
        raise ValidationError({"quantity_milli": "Solde de stock hors capacité."})
    now = timezone.now()
    # A device time slightly ahead of the server (tolerated skew) never dates a sale in the future.
    effective_at = data.get("occurred_at") or min(data.get("recorded_at", now), now)
    return _persist_sale(shop=shop, actor=actor, installation=installation, data=data, canonical=canonical,
        snapshots=snapshots, basket=basket, customer=customer, due_at=due_at,
        remaining=remaining, quantities=quantities, effective_at=effective_at)


def _persist_sale(*, shop, actor, installation, data, canonical, snapshots, basket, customer, due_at,
                  remaining, quantities, effective_at, origin_capture=None, recovery_review=None):
    """Write validated facts under the caller's authorization and transaction.

    Recovery callers use the verified current writer in relational audit fields;
    the protected origin link keeps historical references distinct from rights.
    """
    paid = sum(payment["amount_fcfa"] for payment in data["payments"])
    excess = max(0, paid - basket.net_total_fcfa)
    account = resolve_customer(customer) if customer is not None else None  # G18 redirection
    audit = Activity.objects.create(
        id=data["operation_id"], shop=shop, actor=actor, action="sale_created", target_id=data["id"],
        reason=data.get("reason") or ("Vente à crédit" if data["mode"] == "credit" else "Vente comptant"), details={"request": canonical},
    )
    context = {}
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=audit, capture=origin_capture)
        context["origin_identity"] = {"actor_id": origin_capture.origin_user_id,
            "installation_id": origin_capture.origin_installation_id}
        audit.details["recovery"] = json_value({"capture_id": origin_capture.id,
            "origin_user_id": origin_capture.origin_user_id,"origin_installation_id": origin_capture.origin_installation_id,
            "origin_recorded_at": origin_capture.origin_recorded_at,"review": recovery_review})
    sale = Sale.objects.create(
        id=data["id"], shop=shop, actor=actor, installation=installation, operation=audit, occurred_at=effective_at,
        reference_total_fcfa=basket.reference_total_fcfa, discount_total_fcfa=basket.discount_total_fcfa,
        net_total_fcfa=basket.net_total_fcfa,
        stock_requires_regularization=any(value < 0 for value in remaining),
        mode=data["mode"], customer=customer,
    )
    for position, (snapshot, amount) in enumerate(zip(snapshots, basket.lines, strict=True)):
        line = SaleLine.objects.create(
            sale=sale, position=position, **snapshot, reference_fcfa=amount.reference_fcfa,
            own_discount_fcfa=amount.own_discount_fcfa, global_discount_fcfa=amount.global_discount_fcfa,
            net_fcfa=amount.net_fcfa,
        )
        product_id = line.product_id
        StockMovement.objects.create(
            product_id=product_id, actor=actor, operation=audit, sale_line=line,
            kind="sale", quantity_milli=-line.base_quantity_milli, occurred_at=effective_at, reason=audit.reason,
        )
    freeze_sale_costs(sale)
    for position, payment_data in enumerate(data["payments"]):
        payment = SalePayment.objects.create(sale=sale, position=position,
                                             possible_duplicate_reference=possible_duplicate_reference(shop, payment_data), **payment_data)
        CashMovement.objects.create(
            shop=shop, actor=actor, installation=installation, operation=audit, sale_payment=payment,
            method=payment.method, amount_fcfa=payment.amount_fcfa, occurred_at=effective_at, reason=data.get("reason", "Paiement de vente"),
        )
    if basket.net_total_fcfa > paid:
        CustomerDebt.objects.create(
            id=uuid.uuid5(sale.id, "customer-debt"), shop=shop, customer=account, sale=sale,
            received_for=customer if account is not None and account.id != customer.id else None,
            actor=actor, installation=installation, operation=audit,
            amount_fcfa=basket.net_total_fcfa - paid, remaining_fcfa=basket.net_total_fcfa - paid,
            occurred_at=effective_at, due_at=due_at,
        )
    if customer is not None:
        if excess:
            CustomerCredit.objects.create(
                id=uuid.uuid5(sale.id, "customer-credit"), shop=shop, customer=account, sale=sale, operation=audit,
                amount_fcfa=excess, remaining_fcfa=excess, occurred_at=effective_at,
            )
        append_customer_ledger(shop, account, audit, sale.id, "sale", effective_at, basket.net_total_fcfa,
                               debt_delta_fcfa=max(0, basket.net_total_fcfa - paid), credit_delta_fcfa=excess)
        for position, payment in enumerate(sale.payments.all()):
            append_customer_ledger(shop, account, audit, payment.id, "sale_payment", effective_at,
                                   -payment.amount_fcfa, sequence=position + 1)
    receipt = json_value(SaleSerializer(sale, context=context).data)
    audit.details["response"] = receipt
    audit.save(update_fields=["details", "updated_at"])
    observe_stock(quantities.keys(), audit)
    return receipt, True
