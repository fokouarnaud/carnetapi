"""Recover frozen commercial returns only in a compatible quantity/financial context."""
from dataclasses import dataclass
import hashlib
import uuid
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import APIException,ValidationError
from backups.journal_protocol import JournalDateTimeField,canonical_bytes
from backups.services.recovery_customers import recorded_origin_evidence
from common.api.fields import StrictSerializer,ExactIntegerField
from common.models import Activity
from catalogue.models import Product
from common.services.operations import json_value
from customers.models import CustomerDebt,CustomerLedgerEntry
from identity.models import Installation
from sales.models import Sale,SaleLine,SaleReturn,SaleReturnLine,SaleCancellation
from sales.api.sale_returns import ReturnInput
from sales.services.pricing import MAX_FCFA
from sales.services.return_pricing import return_amount_fcfa
from stock.services.stock_queries import StockTotal


class RecoverySaleReturnInvalid(APIException):
    status_code=400
    default_code="recovery_sale_return_invalid"
    default_detail=_("Recovery commercial return snapshot is inconsistent.")


class FrozenReturnLine(StrictSerializer):
    id=serializers.UUIDField()
    sale_line_id=serializers.UUIDField()
    quantity_milli=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    amount_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    original_quantity_milli=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    original_amount_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    unit_kind=serializers.ChoiceField(choices=Product.UnitKind.choices)


class FrozenSaleReturn(StrictSerializer):
    id=serializers.UUIDField()
    sale_id=serializers.UUIDField()
    customer_id=serializers.UUIDField(allow_null=True)
    amount_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    debt_reduced_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    credit_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    occurred_at=JournalDateTimeField()
    created_at=JournalDateTimeField()
    lines=FrozenReturnLine(many=True,allow_empty=False,max_length=500)


class FrozenSaleReturnPayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    sale_return=FrozenSaleReturn()


class SourceReturnInput(ReturnInput):
    occurred_at=JournalDateTimeField()
    def validate_occurred_at(self,value):return value


@dataclass(frozen=True)
class DecodedSaleReturn:
    request:dict
    snapshot:dict
    canonical_request:dict


def decode_sale_return(capture):
    try:
        incoming=FrozenSaleReturnPayload(data=capture.payload);incoming.is_valid(raise_exception=True)
        snapshot=incoming.validated_data["sale_return"];raw=incoming.validated_data["original_request"]
        if not isinstance(raw,dict):raise RecoverySaleReturnInvalid()
        raw=dict(raw)
        if (uuid.UUID(str(raw.pop("sale_id",snapshot["sale_id"])))!=snapshot["sale_id"]
                or uuid.UUID(str(raw.pop("installation_id",capture.origin_installation_id)))!=capture.origin_installation_id):raise RecoverySaleReturnInvalid()
        request=SourceReturnInput(data=raw);request.is_valid(raise_exception=True);data=request.validated_data
        if (data["id"]!=snapshot["id"] or data["operation_id"]!=capture.operation_id or data["occurred_at"]!=capture.occurred_at
                or snapshot["occurred_at"]!=capture.occurred_at or len(data["lines"])!=len(snapshot["lines"])
                or sum(row["amount_fcfa"] for row in snapshot["lines"])!=snapshot["amount_fcfa"]
                or snapshot["debt_reduced_fcfa"]+snapshot["credit_fcfa"]!=snapshot["amount_fcfa"]):raise RecoverySaleReturnInvalid()
        requested={row["id"]:row for row in data["lines"]}
        if len({row["id"] for row in snapshot["lines"]})!=len(snapshot["lines"]):raise RecoverySaleReturnInvalid()
        for frozen in snapshot["lines"]:
            row=requested[frozen["id"]]
            numerator=frozen["original_amount_fcfa"]*frozen["quantity_milli"]
            floor,remainder=divmod(numerator,frozen["original_quantity_milli"])
            if (any(row[key]!=frozen[key] for key in ("id","sale_line_id","quantity_milli"))
                    or frozen["quantity_milli"]>frozen["original_quantity_milli"]
                    or frozen["unit_kind"]=="piece" and (frozen["quantity_milli"]%1000 or frozen["original_quantity_milli"]%1000)
                    or frozen["amount_fcfa"] not in (floor,floor+(remainder>0))):raise RecoverySaleReturnInvalid()
        canonical=json_value({**data,"sale_id":snapshot["sale_id"],"installation_id":capture.origin_installation_id})
        return DecodedSaleReturn(data,snapshot,canonical)
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):raise RecoverySaleReturnInvalid() from None


def _rows(rows):return sorted(json_value(rows),key=lambda row:row["id"])


def _known_return(shop,capture,decoded,sale,issues,notices):
    operation=Activity.objects.filter(shop=shop,id=capture.operation_id).first()
    if operation is None:
        if Activity.objects.filter(id=capture.operation_id).exists():issues.append({"code":"operation_conflict","ids":[str(capture.operation_id)]})
        return None,False
    snapshot=decoded.snapshot;verified=False
    try:
        if not isinstance(operation.details,dict):raise ValueError()
        actor,device,verified,origin=recorded_origin_evidence(capture,operation,notices,client_metadata=False)
        receipt=operation.details["response"]
        initial=(operation.details["recovery"]["source_snapshot"] if origin else {
            **{key:receipt[key] for key in snapshot if key!="customer_id"},"customer_id":str(sale.customer_id) if sale and sale.customer_id else None})
        frozen_initial=dict(initial);frozen_initial["lines"]=_rows(frozen_initial["lines"])
        expected=json_value(snapshot);expected["lines"]=_rows(expected["lines"])
        stored=SaleReturn.objects.filter(id=snapshot["id"],operation=operation,sale=sale).first() if sale else None
        matches=(stored is not None and actor==capture.origin_user_id and device and operation.action=="sale_return_created"
            and operation.target_id==snapshot["id"] and operation.details.get("request")==decoded.canonical_request
            and frozen_initial==expected and receipt["actor_id"]==str(capture.origin_user_id)
            and receipt["operation_id"]==str(capture.operation_id) and receipt["reason"]==decoded.request["reason"]
            and all(receipt[key]==json_value(snapshot[key]) for key in ("id","sale_id","amount_fcfa","debt_reduced_fcfa","credit_fcfa","occurred_at"))
            and all(getattr(stored,key)==snapshot[key] for key in ("amount_fcfa","debt_reduced_fcfa","credit_fcfa","occurred_at"))
            and receipt["created_at"]==json_value(stored.created_at) and _rows(receipt["lines"])==_rows(snapshot["lines"]))
        if matches:
            rows=list(SaleReturnLine.objects.filter(sale_return=stored,sale_line__sale=sale).values(*FrozenReturnLine().fields))
            matches=_rows(rows)==_rows(snapshot["lines"])
    except (KeyError,ValueError,TypeError,AttributeError):matches=False
    if not matches:issues.append({"code":"operation_conflict","ids":[str(capture.operation_id)]})
    return operation,verified


def build_sale_return_preview(shop,capture):
    decoded=decode_sale_return(capture);snapshot=decoded.snapshot;issues=[];notices=[]
    sale=Sale.objects.filter(shop=shop,id=snapshot["sale_id"]).filter(Q(customer__isnull=True)|Q(customer__shop=shop)).select_related("customer").first()
    known,verified=_known_return(shop,capture,decoded,sale,issues,notices)
    ids=[row["sale_line_id"] for row in snapshot["lines"]]
    lines=SaleLine.objects.filter(sale=sale,id__in=ids).in_bulk() if sale else {}
    totals={row["sale_line_id"]:int(row["total"]) for row in SaleReturnLine.objects.filter(sale_return__sale=sale,sale_line_id__in=ids)
        .order_by().values("sale_line_id").annotate(total=StockTotal("quantity_milli"))} if sale else {}
    debt=CustomerDebt.objects.filter(sale=sale,shop=shop,customer_id=sale.customer_id).first() if sale else None
    cancelled=SaleCancellation.objects.filter(sale=sale).exists() if sale else False
    reduced=min(snapshot["amount_fcfa"],debt.remaining_fcfa) if debt else 0
    contexts=[];prepared=[]
    for row in snapshot["lines"]:
        original=lines.get(row["sale_line_id"]);before=totals.get(row["sale_line_id"],0);expected=None
        if original:
            if before+row["quantity_milli"]<=original.base_quantity_milli:
                expected=return_amount_fcfa(original.net_fcfa,original.base_quantity_milli,before,row["quantity_milli"])
            prepared.append({key:row[key] for key in ("id","quantity_milli","amount_fcfa","original_quantity_milli","original_amount_fcfa","unit_kind")} | {"sale_line":original})
        contexts.append({"sale_line_id":row["sale_line_id"],"returned_before_milli":before if original else None,
            "expected_amount_fcfa":expected,"original_quantity_milli":original.base_quantity_milli if original else None,
            "original_amount_fcfa":original.net_fcfa if original else None})
        if known is None:
            if original is None:issues.append({"code":"missing_sale_line","ids":[str(row["sale_line_id"])]})
            elif (original.base_quantity_milli!=row["original_quantity_milli"] or original.net_fcfa!=row["original_amount_fcfa"]
                    or original.unit_kind!=row["unit_kind"]):issues.append({"code":"return_original_line_conflict","ids":[str(original.id)]})
            elif expected is None:issues.append({"code":"return_quantity_exceeded","ids":[str(original.id)]})
            elif expected!=row["amount_fcfa"]:issues.append({"code":"return_value_context_changed","ids":[str(original.id)]})
    if known is None:
        if capture.occurred_at>timezone.now() or snapshot["created_at"]>timezone.now():issues.append({"code":"future_effective_date","ids":[]})
        if sale is None:issues.append({"code":"missing_sale","ids":[str(snapshot["sale_id"])]})
        else:
            if sale.customer_id!=snapshot["customer_id"]:issues.append({"code":"return_customer_conflict","ids":[]})
            if capture.occurred_at<sale.occurred_at:issues.append({"code":"return_precedes_sale","ids":[]})
            if cancelled:issues.append({"code":"sale_cancelled","ids":[]})
            if debt is None and CustomerDebt.objects.filter(sale=sale).exists():issues.append({"code":"sale_debt_scope_conflict","ids":[]})
            if reduced!=snapshot["debt_reduced_fcfa"] or snapshot["amount_fcfa"]-reduced!=snapshot["credit_fcfa"]:
                issues.append({"code":"return_financial_context_changed","ids":[]})
        ready={str(value) for value in Activity.objects.filter(shop=shop,id__in=capture.dependencies,details__has_key="response").values_list("id",flat=True)}
        missing=sorted(set(capture.dependencies)-ready)
        if missing:issues.append({"code":"missing_dependencies","ids":missing})
        if (SaleReturn.objects.filter(id=snapshot["id"]).exists() or SaleReturnLine.objects.filter(id__in=[row["id"] for row in snapshot["lines"]]).exists()
                or CustomerLedgerEntry.objects.filter(id=uuid.uuid5(snapshot["id"],"customer-ledger:sale_return")).exists()):
            issues.append({"code":"entity_uuid_conflict","ids":[]})
    installation=Installation.objects.filter(id=capture.origin_installation_id).values("user_id").first()
    if installation and installation["user_id"]!=capture.origin_user_id:issues.append({"code":"origin_identity_conflict","ids":[]})
    if not verified and not notices:notices.append({"code":"declared_origin_installation","ids":[]})
    latest=capture.integration_decisions.order_by("-version").first();version=latest.version if latest else 0
    state=None if sale is None else {"sale_id":sale.id,"customer_id":sale.customer_id,"cancelled":cancelled,
        "debt_id":debt.id if debt else None,"debt_remaining_fcfa":debt.remaining_fcfa if debt else 0,
        "projected_debt_reduced_fcfa":0 if known else reduced,"projected_credit_fcfa":0 if known else snapshot["amount_fcfa"]-reduced,"lines":contexts}
    frame=json_value({"capture_id":capture.id,"source_sha256":capture.content_sha256,"version":version,"state":state,
        "issues":issues,"notices":notices,"origin_installation_verified":verified,
        "known_receipt":known.details.get("response") if known and isinstance(known.details,dict) else None})
    preview=json_value({"capture_id":capture.id,"operation_type":"sale_return","version":version,
        "preview_token":hashlib.sha256(canonical_bytes(frame)).hexdigest(),"can_apply":not issues,"outcome":"blocked" if issues else "already_known" if known else "new",
        "hold_id":None,"review_state":None,"review_version":0,"can_hold":False,"can_refuse":False,"snapshot":snapshot,"current_state":state,
        "stocks":[],"cash_delta_by_method_fcfa":{},"new_debt_fcfa":0,"new_customer_credit_fcfa":snapshot["credit_fcfa"] if known is None else 0,
        "origin_installation_verified":verified,"issues":issues,"notices":notices,"origin_user_id":capture.origin_user_id,
        "origin_installation_id":capture.origin_installation_id,"origin_recorded_at":capture.origin_recorded_at})
    return preview,decoded,known,prepared,sale
