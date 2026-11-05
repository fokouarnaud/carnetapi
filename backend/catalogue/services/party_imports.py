"""Customer/supplier CSV rows reuse manual record and opening-debt services."""
import re
import uuid
from django.utils.translation import gettext_lazy
from common.services.csv_sources import read_source, preview_result
from customers.api.client_serializers import CreateClientSerializer
from common.services.operations import json_value
from customers.services.client_services import create_client
from customers.api.debts import OpenDebtSerializer
from customers.services.debt_services import open_debt
from purchasing.api.suppliers import CreateSupplierSerializer, create_supplier
from purchasing.api.purchases import OpeningDebtInput, open_supplier_debt
from purchasing.api.supplier_credits import OpeningBalanceInput, open_supplier_balance


def preview_parties(record):
    optional = {"phone", "opening_debt_fcfa", "opening_occurred_at", "opening_reason"}
    if record.kind == "customers":
        optional.add("due_date")
    else:
        optional.update({"opening_advance_fcfa", "opening_credit_fcfa"})
    rows, file_errors = read_source(record, {"name"}, optional)
    candidates = []
    for row in rows:
        if row["errors"]:
            continue
        raw, errors = row["data"], {}
        line = row["line"]
        entity_type = "customer" if record.kind == "customers" else "supplier"
        entity = {"id": uuid.uuid5(record.id, f"{entity_type}:{line}"), "operation_id": uuid.uuid5(record.id, f"operation:{line}"),
                  "name": raw["name"], "phone": raw.get("phone") or None}
        serializer = CreateClientSerializer(data=entity) if record.kind == "customers" else CreateSupplierSerializer(data=entity)
        if not serializer.is_valid():
            errors.update(serializer.errors)
        opening_balances = []
        if record.kind == "suppliers":
            for balance_kind in ("advance", "credit"):
                field = "opening_" + balance_kind + "_fcfa"
                text = raw.get(field, "")
                if not text:
                    continue
                if len(text) > 30 or not re.fullmatch(r"-?[0-9]+", text):
                    errors[field] = gettext_lazy("Un entier est requis.")
                    continue
                amount = int(text)
                if amount == 0:
                    continue
                opening = {"id": uuid.uuid5(record.id, f"opening_{balance_kind}:{line}"),
                           "operation_id": uuid.uuid5(record.id, f"opening_{balance_kind}_operation:{line}"),
                           "balance_kind": balance_kind, "amount_fcfa": amount,
                           "occurred_at": raw.get("opening_occurred_at", ""), "reason": raw.get("opening_reason", "")}
                balance_serializer = OpeningBalanceInput(data=opening)
                if balance_serializer.is_valid():
                    opening_balances.append(balance_serializer.validated_data)
                else:
                    errors[field] = balance_serializer.errors
        amount_text = raw.get("opening_debt_fcfa", "")
        amount = None
        if amount_text:
            if len(amount_text) <= 30 and re.fullmatch(r"-?[0-9]+", amount_text):
                amount = int(amount_text)
            else:
                errors["opening_debt_fcfa"] = gettext_lazy("Un entier est requis.")
        debt_data = None
        if amount is not None and amount != 0:
            debt = {"id": uuid.uuid5(record.id, f"opening_debt:{line}"),
                    "operation_id": uuid.uuid5(record.id, f"opening_debt_operation:{line}"), "amount_fcfa": amount,
                    "occurred_at": raw.get("opening_occurred_at", ""), "reason": raw.get("opening_reason", "")}
            if record.kind == "customers":
                debt["due_date"] = raw.get("due_date") or None
            debt_serializer = OpenDebtSerializer(data=debt) if record.kind == "customers" else OpeningDebtInput(data=debt)
            if debt_serializer.is_valid():
                debt_data = debt_serializer.validated_data
            else:
                errors["opening_debt"] = debt_serializer.errors
        elif not opening_balances and not errors and any(raw.get(field) for field in ("opening_occurred_at", "opening_reason", "due_date")):
            errors["opening_debt_fcfa"] = gettext_lazy("Une dette positive est requise pour ces champs d'ouverture.")
        if not errors:
            candidate = {"record": serializer.validated_data, "opening_debt": debt_data, "opening_balances": opening_balances}
            candidates.append(candidate)
            row["data"] = json_value(candidate)
        row["errors"] = errors
    return preview_result(record, rows, candidates, file_errors)


def apply_party(kind, shop, request, candidate):
    if kind == "customers":
        receipt, _ = create_client(shop.id, request.user, request.device_session.installation, candidate["record"], request.device_session.id)
    else:
        receipt, _ = create_supplier(shop.id, request, candidate["record"])
    debt_id = None
    if candidate["opening_debt"] is not None:
        if kind == "customers":
            debt, _ = open_debt(shop.id, receipt["id"], request.user, request.device_session.installation,
                                request.device_session.id, candidate["opening_debt"])
        else:
            debt, _ = open_supplier_debt(shop.id, receipt["id"], request, candidate["opening_debt"])
        debt_id = debt["id"]
    balance_ids = []
    for opening in candidate["opening_balances"]:
        balance, _ = open_supplier_balance(shop.id, receipt["id"], request, opening)
        balance_ids.append(balance["id"])
    return receipt, debt_id, balance_ids
