"""Recover surviving physical arrivals against an existing, frozen departure."""
from dataclasses import dataclass
import hashlib
import uuid

from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import APIException, ValidationError

from backups.journal_protocol import JournalDateTimeField, canonical_bytes
from backups.services.recovery_customers import recorded_origin_evidence
from backups.services.recovery_return_receipts import CostIntegerString
from catalogue.models import Product
from common.api.fields import ExactIntegerField, StrictSerializer
from common.models import Activity
from common.services.operations import json_value
from identity.models import Installation
from sales.services.pricing import MAX_FCFA
from stock.models import StockMovement, StockTransfer, StockTransferReceipt, StockTransferReceiptLine
from stock.services.moving_average_cost import returned_cost_nanos
from stock.services.stock_queries import with_stock
from stock.services.transfers import transfer_lines


class RecoveryTransferReceiptInvalid(APIException):
    status_code = 400
    default_code = "recovery_transfer_receipt_invalid"
    default_detail = _("Recovery physical transfer receipt snapshot is inconsistent.")


class FrozenTransferReceiptLine(StrictSerializer):
    id = serializers.UUIDField()
    transfer_line_id = serializers.UUIDField()
    product_id = serializers.UUIDField()
    product_version = ExactIntegerField(min_value=1, max_value=MAX_FCFA)
    product_name = serializers.CharField(max_length=150, trim_whitespace=False)
    unit_kind = serializers.ChoiceField(choices=Product.UnitKind.choices)
    unit_label = serializers.CharField(max_length=40, trim_whitespace=False)
    quantity_milli = ExactIntegerField(min_value=1, max_value=MAX_FCFA)
    received_before_milli = CostIntegerString()
    movement_id = serializers.UUIDField()
    cost_state = serializers.ChoiceField(choices=["known", "provisional", "unknown"])
    total_cost_nanos_fcfa = CostIntegerString(allow_null=True)


class FrozenTransferReceipt(StrictSerializer):
    id = serializers.UUIDField()
    transfer_id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    installation_id = serializers.UUIDField()
    actor_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000, trim_whitespace=False)
    occurred_at = JournalDateTimeField()
    created_at = JournalDateTimeField()
    lines = FrozenTransferReceiptLine(many=True, allow_empty=False, max_length=500)


class TransferReceiptPayload(StrictSerializer):
    schema_version = ExactIntegerField(min_value=1, max_value=1)
    original_request = serializers.JSONField()
    receipt = FrozenTransferReceipt()


@dataclass(frozen=True)
class DecodedTransferReceipt:
    request: dict
    snapshot: dict
    canonical_request: dict


def decode_transfer_receipt(capture):
    """Require each explicit destination and native row/movement identity."""
    from stock.api.transfers import ArrivalInput

    class SourceArrivalInput(ArrivalInput):
        occurred_at = JournalDateTimeField()

        def validate_occurred_at(self, value):
            return value

    try:
        incoming = TransferReceiptPayload(data=capture.payload)
        incoming.is_valid(raise_exception=True)
        snapshot = incoming.validated_data["receipt"]
        raw = incoming.validated_data["original_request"]
        if not isinstance(raw, dict):
            raise RecoveryTransferReceiptInvalid()
        raw = dict(raw)
        if (uuid.UUID(str(raw.pop("transfer_id"))) != snapshot["transfer_id"]
                or uuid.UUID(str(raw.pop("installation_id"))) != capture.origin_installation_id):
            raise RecoveryTransferReceiptInvalid()
        request = SourceArrivalInput(data=raw)
        request.is_valid(raise_exception=True)
        data = request.validated_data
        if (data["id"] != snapshot["id"] or data["operation_id"] != capture.operation_id
                or snapshot["operation_id"] != capture.operation_id
                or snapshot["actor_id"] != capture.origin_user_id
                or snapshot["installation_id"] != capture.origin_installation_id
                or not data["is_sellable_confirmed"] or data["reason"] != snapshot["reason"]
                or data["occurred_at"] != capture.occurred_at or snapshot["occurred_at"] != capture.occurred_at
                or snapshot["created_at"] < capture.occurred_at):
            raise RecoveryTransferReceiptInvalid()
        asked = {row["id"]: row for row in data["lines"]}
        ids = [row["id"] for row in snapshot["lines"]]
        movements = [row["movement_id"] for row in snapshot["lines"]]
        if (len(ids) != len(set(ids)) or set(ids) != set(asked)
                or len(movements) != len(set(movements))):
            raise RecoveryTransferReceiptInvalid()
        for row in snapshot["lines"]:
            if (any(row[key] != asked[row["id"]][key] for key in
                    ("transfer_line_id", "product_id", "quantity_milli"))
                    or int(row["received_before_milli"]) > MAX_FCFA
                    or row["unit_kind"] == "piece" and row["quantity_milli"] % 1000
                    or (row["cost_state"] == "unknown") != (row["total_cost_nanos_fcfa"] is None)):
                raise RecoveryTransferReceiptInvalid()
        canonical = json_value({**data, "transfer_id": snapshot["transfer_id"],
            "installation_id": capture.origin_installation_id})
        return DecodedTransferReceipt(data, snapshot, canonical)
    except (ValidationError, KeyError, ValueError, TypeError, AttributeError, OverflowError):
        raise RecoveryTransferReceiptInvalid() from None


def _departure_line_valid(line, transfer):
    """Foreign keys in a restored archive are references, never authorization."""
    movement, cost = line.movement, line.source_cost
    return (transfer.operation.action == "stock_transfer_departed" and transfer.operation.target_id == transfer.id
        and line.product.shop_id == transfer.source_shop_id
        and movement.product_id == line.product_id and movement.operation_id == transfer.operation_id
        and movement.kind == "transfer_out" and movement.quantity_milli == -line.quantity_milli
        and cost.movement_id == movement.id and cost.version == 1
        and cost.trigger_operation_id == transfer.operation_id)


def _known_receipt(shop, capture, decoded, transfer, lines, issues, notices):
    operation = Activity.objects.filter(id=capture.operation_id, shop=shop).first()
    if operation is None:
        if Activity.objects.filter(id=capture.operation_id).exists():
            issues.append({"code": "operation_conflict", "ids": [str(capture.operation_id)]})
        return None, False
    verified = False
    try:
        actor, device, verified, origin = recorded_origin_evidence(capture, operation, notices, client_metadata=False)
        result = operation.details["response"]
        initial = operation.details["recovery"]["source_snapshot"] if origin else result
        receipt = StockTransferReceipt.objects.filter(id=decoded.snapshot["id"], transfer=transfer, operation=operation).first() if transfer else None
        matches = (receipt is not None and actor == capture.origin_user_id and device
            and initial == json_value(decoded.snapshot) and operation.action == "stock_transfer_received"
            and operation.target_id == decoded.snapshot["id"] and operation.reason == decoded.request["reason"]
            and operation.details.get("request") == decoded.canonical_request
            and receipt.occurred_at == capture.occurred_at and receipt.installation.user_id == operation.actor_id
            and not operation.cash_movements.exists())
        if matches:
            frozen = {row["id"]: row for row in decoded.snapshot["lines"]}
            rows = list(receipt.lines.select_related("movement", "product"))
            matches = len(rows) == len(frozen) and operation.stock_movements.count() == len(rows)
            for row in rows:
                expected = frozen.get(row.id)
                line = lines.get(row.transfer_line_id)
                if expected is None or line is None:
                    matches = False
                    break
                expected_cost = returned_cost_nanos(line.source_cost.total_cost_nanos_fcfa, line.quantity_milli,
                    int(expected["received_before_milli"]), expected["quantity_milli"])
                matches = matches and all(getattr(row, key) == expected[key] for key in
                    ("transfer_line_id", "product_id", "product_version", "product_name", "unit_kind", "unit_label", "quantity_milli", "movement_id", "cost_state"))
                matches = (matches and row.product.shop_id == shop.id
                    and row.received_before_milli == int(expected["received_before_milli"])
                    and row.total_cost_nanos_fcfa == expected_cost
                    and row.total_cost_nanos_fcfa == (None if expected["total_cost_nanos_fcfa"] is None else int(expected["total_cost_nanos_fcfa"]))
                    and row.cost_state == line.source_cost.cost_state
                    and row.movement.operation_id == operation.id and row.movement.actor_id == operation.actor_id
                    and row.movement.product_id == row.product_id and row.movement.kind == "transfer_in"
                    and row.movement.quantity_milli == row.quantity_milli
                    and row.movement.occurred_at == capture.occurred_at and row.movement.reason == decoded.request["reason"])
            from stock.api.transfers import TransferReceiptSerializer
            matches = matches and result == json_value(TransferReceiptSerializer(receipt).data)
    except (KeyError, ValueError, TypeError, AttributeError):
        matches = False
    if not matches:
        issues.append({"code": "operation_conflict", "ids": [str(capture.operation_id)]})
    return operation, verified


def build_transfer_receipt_preview(shop, capture):
    decoded = decode_transfer_receipt(capture)
    snapshot = decoded.snapshot
    issues, notices, prepared = [], [], []
    transfer = StockTransfer.objects.filter(id=snapshot["transfer_id"], destination_shop=shop,
        source_shop__owner_id=shop.owner_id, operation__shop_id=F("source_shop_id")).exclude(source_shop=shop).select_related("operation").first()
    lines = ({row.id: row for row in transfer_lines().filter(transfer=transfer).select_related(
        "product", "movement", "source_cost")} if transfer else {})
    if any(not _departure_line_valid(line, transfer) for line in lines.values()):
        issues.append({"code": "transfer_departure_relation_conflict", "ids": []})
        lines = {}
    known, verified = _known_receipt(shop, capture, decoded, transfer, lines, issues, notices)
    products = Product.objects.filter(shop=shop, id__in=[row["product_id"] for row in snapshot["lines"]]).in_bulk()
    current_lines = []
    before = {identity: int(line.received_milli) for identity, line in lines.items()}
    for line in lines.values():
        current_lines.append({"line_id": line.id, "quantity_milli": line.quantity_milli,
            "received_milli": str(before[line.id]), "source_cost_id": line.source_cost_id,
            "cost_state": line.source_cost.cost_state, "total_cost_nanos_fcfa": str(line.source_cost.total_cost_nanos_fcfa) if line.source_cost.total_cost_nanos_fcfa is not None else None})
    if known is None:
        if transfer is None:
            issues.append({"code": "missing_transfer_departure", "ids": [str(snapshot["transfer_id"])]})
        if capture.occurred_at > timezone.now() or snapshot["created_at"] > timezone.now():
            issues.append({"code": "future_effective_date", "ids": []})
        if transfer and capture.occurred_at < transfer.occurred_at:
            issues.append({"code": "receipt_precedes_departure", "ids": []})
        ready = {str(value) for value in Activity.objects.filter(shop=shop, id__in=capture.dependencies,
            details__has_key="response").values_list("id", flat=True)}
        missing = sorted(set(capture.dependencies) - ready)
        if missing:
            issues.append({"code": "missing_dependencies", "ids": missing})
        if (StockTransferReceipt.objects.filter(id=snapshot["id"]).exists()
                or StockTransferReceiptLine.objects.filter(id__in=[row["id"] for row in snapshot["lines"]]).exists()
                or StockMovement.objects.filter(id__in=[row["movement_id"] for row in snapshot["lines"]]).exists()):
            issues.append({"code": "entity_uuid_conflict", "ids": []})
    frozen_rows = {row["id"]: row for row in snapshot["lines"]}
    deltas = {}
    for asked in decoded.request["lines"]:
        frozen = frozen_rows[asked["id"]]
        product, line = products.get(frozen["product_id"]), lines.get(frozen["transfer_line_id"])
        if known is not None:
            if product:
                deltas[product.id] = 0
            continue
        if product is None:
            issues.append({"code": "missing_destination_product", "ids": [str(frozen["product_id"])]})
        if line is None:
            issues.append({"code": "missing_transfer_line", "ids": [str(frozen["transfer_line_id"])]})
        if product is None or line is None:
            continue
        previous = before[line.id]
        allocated = None
        try:
            allocated = returned_cost_nanos(line.source_cost.total_cost_nanos_fcfa, line.quantity_milli,
                int(frozen["received_before_milli"]), frozen["quantity_milli"])
        except ValueError:
            issues.append({"code": "transfer_receipt_cost_context_invalid", "ids": [str(line.id)]})
        amount = None if frozen["total_cost_nanos_fcfa"] is None else int(frozen["total_cost_nanos_fcfa"])
        if ((product.unit_kind, product.unit_label) != (frozen["unit_kind"], frozen["unit_label"])
                or (line.unit_kind, line.unit_label) != (frozen["unit_kind"], frozen["unit_label"])):
            issues.append({"code": "transfer_receipt_unit_mismatch", "ids": [str(product.id)]})
        if previous != int(frozen["received_before_milli"]):
            issues.append({"code": "transfer_receipt_context_changed", "ids": [str(line.id)]})
        if previous + frozen["quantity_milli"] > line.quantity_milli:
            issues.append({"code": "transfer_receipt_quantity_exceeded", "ids": [str(line.id)]})
        if amount != allocated or frozen["cost_state"] != line.source_cost.cost_state:
            issues.append({"code": "transfer_receipt_cost_mismatch", "ids": [str(line.id)]})
        adjustments = list(StockMovement.objects.filter(product=product, kind="inventory_adjustment",
            occurred_at__gte=capture.occurred_at, operation__created_at__gte=capture.session.generation.created_at).values_list("operation_id", flat=True))
        if adjustments:
            issues.append({"code": "existing_regularization_review_required", "ids": [str(value) for value in adjustments]})
        deltas[product.id] = deltas.get(product.id, 0) + frozen["quantity_milli"]
        before[line.id] += frozen["quantity_milli"]
        prepared.append((asked, line, product, int(frozen["received_before_milli"]), allocated))
    device = Installation.objects.filter(id=capture.origin_installation_id).values("user_id").first()
    if device and device["user_id"] != capture.origin_user_id:
        issues.append({"code": "origin_identity_conflict", "ids": []})
    if not verified:
        notices.append({"code": "declared_origin_installation", "ids": []})
    stocks = []
    for product in with_stock(Product.objects.filter(shop=shop, id__in=deltas)).order_by("id"):
        delta = deltas[product.id]
        after = int(product.stock_validated_milli) + delta
        if after > MAX_FCFA:
            issues.append({"code": "stock_balance_out_of_range", "ids": [str(product.id)]})
        stocks.append({"product_id": product.id, "reservation_id": None,
            "validated_before_milli": product.stock_validated_milli, "reserved_before_milli": product.stock_reserved_milli,
            "available_before_milli": product.stock_available_milli, "validated_after_milli": str(after),
            "reserved_after_milli": product.stock_reserved_milli, "available_after_milli": str(int(product.stock_available_milli) + delta)})
    latest = capture.integration_decisions.order_by("-version").first()
    version = latest.version if latest else 0
    current = None if transfer is None else json_value({"transfer_id": transfer.id,
        "departure_operation_id": transfer.operation_id, "departure_occurred_at": transfer.occurred_at,
        "departure_updated_at": transfer.updated_at, "lines": current_lines,
        "destination_products": [{"id": p.id, "version": p.version, "unit_kind": p.unit_kind, "unit_label": p.unit_label,
            "updated_at": p.updated_at} for p in sorted(products.values(), key=lambda p: str(p.id))]})
    frame = json_value({"capture_id": capture.id, "source_sha256": capture.content_sha256, "version": version,
        "state": current, "stocks": stocks, "issues": issues, "notices": notices,
        "origin_installation_verified": verified, "known_receipt": known.details.get("response") if known else None})
    preview = json_value({"capture_id": capture.id, "operation_type": "transfer_receipt", "version": version,
        "preview_token": hashlib.sha256(canonical_bytes(frame)).hexdigest(), "can_apply": not issues,
        "outcome": "blocked" if issues else "already_known" if known else "new", "hold_id": None,
        "review_state": None, "review_version": 0, "can_hold": False, "can_refuse": False,
        "snapshot": snapshot, "current_state": current, "stocks": stocks, "cash_delta_by_method_fcfa": {},
        "new_debt_fcfa": 0, "new_customer_credit_fcfa": 0, "origin_user_id": capture.origin_user_id,
        "origin_installation_id": capture.origin_installation_id, "origin_recorded_at": capture.origin_recorded_at,
        "origin_installation_verified": verified, "issues": issues, "notices": notices})
    return preview, decoded, known, prepared, transfer
