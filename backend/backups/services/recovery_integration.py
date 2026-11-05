"""Apply reviewed frozen facts, recognizing already committed operation UUIDs."""
import hashlib
import uuid

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied,APIException

from backups.journal_protocol import canonical_bytes
from backups.models import RecoveryJournalOperation,RecoveryIntegrationDecision,RestoreAccessGeneration,RecoverySaleHold
from backups.services.recovery_journal import journal_context,JournalAccessDenied
from backups.services.recovery_sale_snapshot import decode_sale_snapshot,immutable_receipt_snapshot
from catalogue.models import Product,ProductPack
from common.api.fields import ExactIntegerField,StrictSerializer
from common.models import Activity
from common.services.errors import OperationConflict
from common.services.operations import json_value
from customers.models import Customer
from identity.models import Installation
from sales.models import Sale,SaleLine,SalePayment
from sales.services.pricing import MAX_FCFA
from sales.services.sale_services import _persist_sale
from stock.models import StockMovement,StockReservation
from stock.services.stock_queries import with_stock
from backups.services.recovery_sale_holds import find_sale_hold,complete_sale_hold


class RecoveryIntegrationDenied(PermissionDenied):
    default_detail=_("Only a current shop responsible can integrate recovery operations.")


class RecoveryTypeUnsupported(APIException):
    status_code=400
    default_code="recovery_type_unsupported"
    default_detail=_("This recovery operation type is not supported yet.")


class RecoveryIntegrationInput(StrictSerializer):
    operation_id=serializers.UUIDField()
    previous_version=ExactIntegerField(min_value=0,max_value=MAX_FCFA-1)
    preview_token=serializers.RegexField(r"^[0-9a-f]{64}$")
    reason=serializers.CharField(max_length=2000,allow_blank=False)


def integration_context(shop_id,actor,session_id):
    try:return journal_context(shop_id,actor,session_id)
    except JournalAccessDenied:raise RecoveryIntegrationDenied() from None


def _capture(shop,capture_id):
    capture=get_object_or_404(RecoveryJournalOperation.objects.select_related("session__generation"),
        id=capture_id,shop=shop,session__generation__current_slot=1)
    try:
        entry=json_value({"operation_id":capture.operation_id,"origin_installation_id":capture.origin_installation_id,
            "origin_user_id":capture.origin_user_id,"operation_type":capture.operation_type,"occurred_at":capture.occurred_at,
            "origin_recorded_at":capture.origin_recorded_at,"payload":capture.payload,"dependencies":capture.dependencies})
        digest=hashlib.sha256(canonical_bytes({"shop_id":str(shop.id),"format_version":1,**entry})).hexdigest()
    except (ValueError,TypeError,UnicodeError,RecursionError):raise OperationConflict() from None
    if digest!=capture.content_sha256:raise OperationConflict()
    return capture


def _known_sale(capture,decoded,issues):
    operation=Activity.objects.filter(id=capture.operation_id,shop_id=capture.shop_id).first()
    if operation is None:
        if Activity.objects.filter(id=capture.operation_id).exists():
            issues.append({"code":"operation_conflict","ids":[str(capture.operation_id)]})
        return None
    receipt=operation.details.get("response")
    try:
        matches=(operation.shop_id==capture.shop_id and operation.action=="sale_created"
            and operation.target_id==decoded.snapshot["id"] and operation.details.get("request")==decoded.canonical_request
            and receipt["actor_id"]==str(capture.origin_user_id) and receipt["installation_id"]==str(capture.origin_installation_id)
            and receipt["occurred_at"]==json_value(capture.occurred_at)
            and immutable_receipt_snapshot(receipt)==json_value(decoded.snapshot)
            and Sale.objects.filter(operation=operation,id=decoded.snapshot["id"],shop_id=capture.shop_id).exists())
    except (KeyError,TypeError,ValueError):matches=False
    if not matches:issues.append({"code":"operation_conflict","ids":[str(capture.operation_id)]})
    return operation


def _build_operation_preview(shop,capture):
    if capture.operation_type in ('product_update','pack_update'):
        from backups.services.recovery_catalogue_revisions import build_catalogue_revision_preview
        return build_catalogue_revision_preview(shop,capture)
    if capture.operation_type=='purchase_receipt':
        from backups.services.recovery_purchase_receipts import build_purchase_receipt_preview
        return build_purchase_receipt_preview(shop,capture)
    if capture.operation_type=='transfer_receipt':
        from backups.services.recovery_transfer_receipts import build_transfer_receipt_preview
        return build_transfer_receipt_preview(shop,capture)
    if capture.operation_type in ('purchase_order','purchase_invoice'):
        from backups.services.recovery_purchases import build_purchase_preview
        return build_purchase_preview(shop,capture)
    if capture.operation_type in ('supplier_payment','supplier_payment_allocation'):
        from backups.services.recovery_supplier_payments import build_supplier_payment_preview
        return build_supplier_payment_preview(shop,capture)
    if capture.operation_type in ('supplier_creation','supplier_opening_debt','supplier_opening_balance'):
        from backups.services.recovery_suppliers import build_supplier_operation_preview
        return build_supplier_operation_preview(shop,capture)
    if capture.operation_type=='sale_cancellation':
        from backups.services.recovery_sale_cancellations import build_sale_cancellation_preview
        return build_sale_cancellation_preview(shop,capture)
    if capture.operation_type=='non_sellable_decision':
        from backups.services.recovery_unsellable_decisions import build_unsellable_decision_preview
        return build_unsellable_decision_preview(shop,capture)
    if capture.operation_type=='return_receipt':
        from backups.services.recovery_return_receipts import build_return_receipt_preview
        return build_return_receipt_preview(shop,capture)
    if capture.operation_type=="sale_return":
        from backups.services.recovery_sale_returns import build_sale_return_preview
        return build_sale_return_preview(shop,capture)
    if capture.operation_type=="return_credit_settlement":
        from backups.services.recovery_return_settlements import build_return_settlement_preview
        return build_return_settlement_preview(shop,capture)
    if capture.operation_type=="customer_allocation":
        from backups.services.recovery_customer_allocations import build_customer_allocation_preview
        return build_customer_allocation_preview(shop,capture)
    if capture.operation_type=="customer_refund":
        from backups.services.recovery_customer_refunds import build_customer_refund_preview
        return build_customer_refund_preview(shop,capture)
    if capture.operation_type in ("customer_update","customer_due_change"):
        from backups.services.recovery_customer_revisions import build_customer_revision_preview
        return build_customer_revision_preview(shop,capture)
    if capture.operation_type in ("customer_creation","customer_opening_debt"):
        from backups.services.recovery_customers import build_customer_operation_preview
        return build_customer_operation_preview(shop,capture)
    if capture.operation_type=="customer_collection":
        from backups.services.recovery_collections import build_collection_preview
        return build_collection_preview(shop,capture)
    if capture.operation_type!="sale":
        raise RecoveryTypeUnsupported()
    decoded=decode_sale_snapshot(capture,timezone_name=shop.timezone)
    issues=[]
    known=_known_sale(capture,decoded,issues)
    latest=capture.integration_decisions.order_by("-version").first()
    version=latest.version if latest else 0
    snapshot=decoded.snapshot
    quantities={}
    for row in snapshot["lines"]:
        quantities[row["product_id"]]=quantities.get(row["product_id"],0)+row["base_quantity_milli"]
    hold=find_sale_hold(capture,quantities)
    if hold is not None:
        paid=sum(payment["amount_fcfa"] for payment in snapshot["payments"])
        if hold.net_total_fcfa!=snapshot["net_total_fcfa"] or hold.payments_fcfa!=paid or hold.sale_id!=snapshot["id"]:
            raise OperationConflict()
        if hold.state=="refused":issues.append({"code":"recovery_sale_refused","ids":[str(hold.id)]})
        if hold.state=="validated" and known is None:issues.append({"code":"validated_recovery_sale_missing","ids":[str(hold.id)]})
    if any(value>MAX_FCFA for value in quantities.values()):
        issues.append({"code":"stock_quantity_out_of_range","ids":[]})
    products={row["id"]:row for row in with_stock(Product.objects.filter(shop=shop,id__in=quantities)).values(
        "id","version","sale_price_fcfa","unit_kind","is_active","stock_threshold_milli",
        "stock_validated_milli","stock_reserved_milli","stock_available_milli")}
    packs={row["id"]:row for row in ProductPack.objects.filter(product__shop=shop,
        id__in=[line["pack_id"] for line in snapshot["lines"] if line["pack_id"]]).values("id","product_id","base_quantity_milli","sale_price_fcfa","is_active")}
    customer=Customer.objects.filter(shop=shop,id=snapshot["customer_id"]).first() if snapshot["customer_id"] else None
    if known is None:
        if capture.occurred_at>timezone.now():issues.append({"code":"future_effective_date","ids":[]})
        for row in snapshot["lines"]:
            product=products.get(row["product_id"])
            if product is None:issues.append({"code":"missing_product","ids":[str(row["product_id"])]})
            elif product["unit_kind"]!=row["unit_kind"]:issues.append({"code":"unit_kind_changed","ids":[str(row["product_id"])]})
            if row["pack_id"] is not None and (row["pack_id"] not in packs or packs[row["pack_id"]]["product_id"]!=row["product_id"]):
                issues.append({"code":"missing_pack","ids":[str(row["pack_id"])]})
        if snapshot["customer_id"] is not None and customer is None:
            issues.append({"code":"missing_customer","ids":[str(snapshot["customer_id"])]})
        ready={str(value) for value in Activity.objects.filter(shop=shop,id__in=capture.dependencies,
            details__has_key="response").values_list("id",flat=True)}
        missing=sorted(set(capture.dependencies)-ready)
        if missing:issues.append({"code":"missing_dependencies","ids":missing})
        if (Sale.objects.filter(id=snapshot["id"]).exists()
                or RecoverySaleHold.objects.filter(sale_id=snapshot["id"]).exclude(source_operation_id=capture.operation_id).exists()
                or SaleLine.objects.filter(id__in=[line["id"] for line in snapshot["lines"]]).exists()
                or SalePayment.objects.filter(id__in=[payment["id"] for payment in snapshot["payments"]]).exists()):
            issues.append({"code":"entity_uuid_conflict","ids":[]})
        adjustments=list(StockMovement.objects.filter(product__shop=shop,product_id__in=quantities,kind="inventory_adjustment",
            occurred_at__gte=capture.occurred_at,operation__created_at__gte=capture.session.generation.created_at)
            .order_by("id").values_list("operation_id",flat=True).distinct())
        if adjustments:issues.append({"code":"existing_regularization_review_required","ids":[str(value) for value in adjustments]})
        source_installation=Installation.objects.filter(id=capture.origin_installation_id).values("user_id").first()
        if source_installation is not None and source_installation["user_id"]!=capture.origin_user_id:
            issues.append({"code":"origin_identity_conflict","ids":[]})
    reservations_by_product=dict(StockReservation.objects.filter(operation_id=hold.reservation_operation_id)
        .values_list("product_id","id")) if hold else {}
    stocks=[]
    for product_id,quantity in sorted(quantities.items(),key=lambda item:str(item[0])):
        row=products.get(product_id)
        if row is not None:
            delta=quantity if known is None else 0
            released=quantity if hold is not None and hold.state=="pending" else 0
            after=int(row["stock_validated_milli"])-delta
            if after< -MAX_FCFA-1:issues.append({"code":"stock_balance_out_of_range","ids":[str(product_id)]})
            stocks.append({"product_id":str(product_id),"reservation_id":reservations_by_product.get(product_id),"validated_before_milli":row["stock_validated_milli"],
                "reserved_before_milli":row["stock_reserved_milli"],"available_before_milli":row["stock_available_milli"],
                "validated_after_milli":str(after),"reserved_after_milli":str(int(row["stock_reserved_milli"])-released),
                "available_after_milli":str(int(row["stock_available_milli"])-delta+released)})
    delta_by_method={}
    if known is None:
        for payment in snapshot["payments"]:
            key="other:"+payment["other_name"] if payment["method"]=="other" else payment["method"]
            delta_by_method[key]=delta_by_method.get(key,0)+payment["amount_fcfa"]
    paid=sum(payment["amount_fcfa"] for payment in snapshot["payments"])
    frame=json_value({"capture_id":capture.id,"source_sha256":capture.content_sha256,"version":version,
        "timezone":shop.timezone,"known_operation":known.id if known else None,"known_receipt":known.details.get("response") if known else None,
        "products":sorted(products.values(),key=lambda row:str(row["id"])),"packs":sorted(packs.values(),key=lambda row:str(row["id"])),
        "customer":str(customer.id) if customer else None,"issues":issues,
        "hold":None if hold is None else {"id":hold.id,"state":hold.state,"version":hold.version}})
    token=hashlib.sha256(canonical_bytes(frame)).hexdigest()
    result=json_value({"capture_id":capture.id,"operation_type":"sale","version":version,"preview_token":token,
        "can_apply":not issues,"outcome":"blocked" if issues else "already_known" if known else "new",
        "hold_id":hold.id if hold else None,"review_state":hold.state if hold else None,"review_version":hold.version if hold else 0,
        "can_hold":not issues and known is None and (hold is None or hold.state=="pending"),
        "can_refuse":hold is not None and hold.state=="pending" and known is None,
        "snapshot":snapshot,"stocks":stocks,"cash_delta_by_method_fcfa":delta_by_method,
        "new_debt_fcfa":max(0,snapshot["net_total_fcfa"]-paid) if known is None else 0,
        "new_customer_credit_fcfa":max(0,paid-snapshot["net_total_fcfa"]) if known is None else 0,
        "origin_user_id":capture.origin_user_id,"origin_installation_id":capture.origin_installation_id,
        "origin_recorded_at":capture.origin_recorded_at,"issues":issues})
    return result,decoded,known,quantities,customer


def _build_preview(shop,capture):
    """Expose closure restrictions in every adapter, including pending reviews."""
    result,decoded,known,quantities,customer=_build_operation_preview(shop,capture)
    from backups.models import RecoveryClosure
    if known is None and (RecoveryClosure.objects.filter(operation_id=capture.operation_id).exists()
            or RecoveryIntegrationDecision.objects.filter(operation_id=capture.operation_id).exists()):
        result['issues'].append({'code':'operation_conflict','ids':[str(capture.operation_id)]})
        result.update(can_apply=False,can_hold=False,can_refuse=False,outcome='blocked')
        if 'can_mark_pending' in result:result['can_mark_pending']=False
        result['preview_token']=hashlib.sha256(canonical_bytes({
            'source_preview_token':result['preview_token'],'reserved_source_operation_id':str(capture.operation_id)})).hexdigest()
    closure=RecoveryClosure.objects.filter(session_id=capture.session_id).only('id').first()
    if closure is not None:
        result['issues'].append({'code':'recovery_closed_review_required','ids':[str(closure.id)]})
        result.update(can_apply=False,can_hold=False,can_refuse=False,outcome='blocked')
        if 'can_mark_pending' in result:result['can_mark_pending']=False
        result['preview_token']=hashlib.sha256(canonical_bytes({
            'source_preview_token':result['preview_token'],'closure_id':str(closure.id)})).hexdigest()
    return result,decoded,known,quantities,customer


@transaction.atomic
def preview_recovery_integration(*,shop_id,actor,session_id,capture_id):
    shop,_=integration_context(shop_id,actor,session_id)
    return _build_preview(shop,_capture(shop,capture_id))[0]


@transaction.atomic
def integrate_recovery_operation(*,shop_id,actor,session_id,capture_id,data):
    shop,live=integration_context(shop_id,actor,session_id)
    if not RestoreAccessGeneration.objects.filter(current_slot=1).update(current_slot=1):raise OperationConflict()
    shop,live=integration_context(shop_id,actor,session_id)
    capture=_capture(shop,capture_id)
    incoming=RecoveryIntegrationInput(data=data)
    incoming.is_valid(raise_exception=True)
    request=json_value(incoming.validated_data)
    previous=RecoveryIntegrationDecision.objects.filter(operation_id=request["operation_id"]).first()
    if previous is not None:
        if (previous.capture_id!=capture.id or previous.reviewed_by_id!=actor.id
                or previous.reviewed_installation_id!=live.installation_id or previous.request!=request):raise OperationConflict()
        return previous.receipt,False
    from backups.models import RecoverySaleHoldDecision,RecoveryClosure
    if (RecoverySaleHoldDecision.objects.filter(operation_id=request["operation_id"]).exists()
            or RecoveryClosure.objects.filter(operation_id=request["operation_id"]).exists()
            or Activity.objects.filter(id=request["operation_id"]).exists()
            or RecoveryJournalOperation.objects.filter(operation_id=request["operation_id"]).exists()):
        raise OperationConflict()
    preview,decoded,known,quantities,customer=_build_preview(shop,capture)
    # The source UUID also becomes an Activity UUID, independent of review UUIDs.
    if known is None and (RecoveryClosure.objects.filter(operation_id=capture.operation_id).exists()
            or RecoveryIntegrationDecision.objects.filter(operation_id=capture.operation_id).exists()):
        raise OperationConflict()
    if (not preview["can_apply"] or preview["preview_token"]!=request["preview_token"]
            or preview["version"]!=request["previous_version"]):raise OperationConflict()
    hold=find_sale_hold(capture,quantities) if capture.operation_type=="sale" else None
    collection_review=None
    if capture.operation_type=="customer_collection":
        from backups.services.recovery_collection_reviews import find_collection_review
        collection_review=find_collection_review(capture,decoded.snapshot)
    if hold is not None and hold.state=="pending":
        if request["operation_id"]==str(capture.operation_id) or Activity.objects.filter(id=request["operation_id"]).exists():
            raise OperationConflict()
        StockReservation.objects.filter(operation=hold.reservation_operation).update(state="validated")
    related_decisions=[]
    if known is not None:
        if capture.operation_type=='sale_cancellation':
            from backups.services.recovery_sale_cancellations import recover_cancellation_child
            _returned,related_decisions=recover_cancellation_child(shop=shop,actor=actor,live=live,capture=capture,
                decoded=decoded,plan=quantities,request=request)
        business=known.details["response"]
        outcome="already_known"
    elif capture.operation_type in ('product_update','pack_update'):
        from backups.services.recovery_catalogue_revisions import persist_catalogue_revision
        business=persist_catalogue_revision(shop=shop,actor=actor,installation=live.installation,
            decoded=decoded,origin_capture=capture,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id);outcome='applied'
    elif capture.operation_type=='purchase_receipt':
        from purchasing.services.receipts import _persist_purchase_receipt
        business,_=_persist_purchase_receipt(shop=shop,document=customer,actor=actor,installation=live.installation,
            data=decoded.request,canonical=decoded.canonical_request,prepared=quantities,
            origin_capture=capture,source_snapshot=decoded.snapshot,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id);outcome='applied'
    elif capture.operation_type=='transfer_receipt':
        from stock.services.transfers import _persist_transfer_receipt
        business,_=_persist_transfer_receipt(shop=shop,transfer=customer,actor=actor,installation=live.installation,
            data=decoded.request,canonical=decoded.canonical_request,prepared=quantities,
            origin_capture=capture,source_snapshot=decoded.snapshot,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id);outcome='applied'
    elif capture.operation_type in ('purchase_order','purchase_invoice'):
        from purchasing.api.purchases import _persist_purchase_document
        from purchasing.models import PurchaseDocument
        order=(PurchaseDocument.objects.filter(id=decoded.snapshot['order_id'],shop=shop,supplier=customer,
            kind='order',operation__shop=shop).first() if decoded.snapshot['order_id'] else None)
        business,_=_persist_purchase_document(shop=shop,supplier=customer,actor=actor,installation=live.installation,
            kind=decoded.snapshot['kind'],data=decoded.request,canonical=decoded.canonical_request,
            snapshots=quantities,order=order,origin_capture=capture,source_snapshot=decoded.snapshot,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id);outcome='applied'
    elif capture.operation_type=='supplier_payment':
        from purchasing.api.supplier_payments import _persist_supplier_payment
        business,_=_persist_supplier_payment(shop=shop,supplier=customer,actor=actor,installation=live.installation,
            data=decoded.request,canonical=decoded.canonical_request,origin_capture=capture,source_snapshot=decoded.snapshot,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id);outcome='applied'
    elif capture.operation_type=='supplier_payment_allocation':
        from purchasing.api.supplier_payments import _persist_supplier_payment_allocation
        business,_=_persist_supplier_payment_allocation(shop=shop,supplier=customer.supplier,payment=customer,actor=actor,
            installation=live.installation,data=decoded.request,canonical=decoded.canonical_request,prepared=quantities,
            source_row_ids=[row['id'] for row in decoded.snapshot['allocations']],effective_at=capture.occurred_at,
            origin_capture=capture,source_snapshot=decoded.snapshot,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id);outcome='applied'
    elif capture.operation_type in ('supplier_creation','supplier_opening_debt','supplier_opening_balance'):
        if capture.operation_type=='supplier_creation':
            from purchasing.api.suppliers import _persist_supplier
            business,_=_persist_supplier(shop=shop,actor=actor,installation=live.installation,data=decoded.request,
                canonical=decoded.canonical_request,origin_capture=capture,source_snapshot=decoded.snapshot,recovery_review=request)
        else:
            from purchasing.api.purchases import _persist_supplier_debt
            from purchasing.api.supplier_credits import _persist_supplier_opening_balance
            persist=_persist_supplier_debt if capture.operation_type=='supplier_opening_debt' else _persist_supplier_opening_balance
            business,_=persist(shop=shop,supplier=customer,actor=actor,installation=live.installation,data=decoded.request,
                canonical=decoded.canonical_request,origin_capture=capture,source_snapshot=decoded.snapshot,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id);outcome='applied'
    elif capture.operation_type=='sale_cancellation':
        from backups.services.recovery_sale_cancellations import recover_cancellation_child
        from sales.api.sale_cancellations import _persist_sale_cancellation
        def append_compensating_return():
            return recover_cancellation_child(shop=shop,actor=actor,live=live,capture=capture,decoded=decoded,plan=quantities,request=request)
        business,_=_persist_sale_cancellation(shop=shop,sale=customer,actor=actor,installation=live.installation,
            data=decoded.request,canonical=decoded.canonical_request,return_writer=append_compensating_return if decoded.child else None,
            origin_capture=capture,source_snapshot=decoded.snapshot,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id);outcome='applied'
        related_decisions=known.details['recovery']['related_integration_decision_ids']
    elif capture.operation_type=='non_sellable_decision':
        from stock.api.unsellable_decisions import _persist_non_sellable_decision
        business,_=_persist_non_sellable_decision(shop=shop,line=customer.return_line,receipt=customer,
            actor=actor,installation=live.installation,data=decoded.request,canonical=decoded.canonical_request,
            **quantities,origin_capture=capture,source_snapshot=decoded.snapshot,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id);outcome='applied'
    elif capture.operation_type=='return_receipt':
        from stock.api.return_receipts import _persist_return_receipt
        business,_=_persist_return_receipt(shop=shop,line=customer,actor=actor,installation=live.installation,
            data=decoded.request,canonical=decoded.canonical_request,cost_values=quantities,origin_capture=capture,
            source_snapshot=decoded.snapshot,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id);outcome='applied'
    elif capture.operation_type=="sale_return":
        from sales.api.sale_returns import _persist_sale_return
        from customers.models import CustomerDebt
        sale=customer
        debt=CustomerDebt.objects.filter(sale=sale,shop=shop,customer_id=sale.customer_id).first()
        business,_=_persist_sale_return(shop=shop,sale=sale,actor=actor,installation=live.installation,data=decoded.request,
            canonical=decoded.canonical_request,snapshots=quantities,debt=debt,origin_capture=capture,
            source_snapshot=decoded.snapshot,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id);outcome="applied"
    elif capture.operation_type=="return_credit_settlement":
        from customers.api.return_credit_settlements import _persist_return_credit_settlement
        from sales.models import SaleReturn
        returned=SaleReturn.objects.select_related("sale__customer").get(id=decoded.snapshot["sale_return_id"],
            sale_id=decoded.snapshot["sale_id"],sale__shop=shop)
        business,_=_persist_return_credit_settlement(shop=shop,sale=returned.sale,returned=returned,actor=actor,
            installation=live.installation,data=decoded.request,kind=decoded.snapshot["kind"],canonical=decoded.canonical_request,
            origin_capture=capture,source_snapshot=decoded.snapshot,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id);outcome="applied"
    elif capture.operation_type in ("customer_refund","customer_allocation"):
        from customers.api.regularizations import _persist_regularization
        from customers.models import CustomerCredit,CustomerPayment
        model=CustomerPayment if decoded.source_kind=="payment" else CustomerCredit
        source=model.objects.get(shop=shop,customer=customer,id=decoded.snapshot[decoded.source_kind+"_id"])
        business,_=_persist_regularization(shop=shop,customer=customer,payment=source,actor=actor,
            installation=live.installation,kind="refund" if capture.operation_type=="customer_refund" else "allocation",data=decoded.request,source_kind=decoded.source_kind,
            canonical=decoded.canonical_request,effective_at=capture.occurred_at,
            origin_capture=capture,recovery_review=request,source_snapshot=decoded.snapshot)
        known=Activity.objects.get(id=capture.operation_id);outcome="applied"
    elif capture.operation_type=="customer_update":
        from customers.services.client_services import _persist_client_update
        business=_persist_client_update(shop=shop,customer=customer,actor=actor,installation=live.installation,data=decoded.request,
            canonical=decoded.canonical_request,source_snapshot=decoded.snapshot,origin_capture=capture,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id);outcome="applied"
    elif capture.operation_type=="customer_due_change":
        from customers.services.debt_services import _persist_debt_due_change
        from customers.models import CustomerDebt
        debt=CustomerDebt.objects.get(shop=shop,customer=customer,id=decoded.snapshot["id"])
        business=_persist_debt_due_change(shop=shop,customer=customer,debt=debt,actor=actor,installation=live.installation,
            data=decoded.request,canonical=decoded.canonical_request,deadline=decoded.snapshot["after_due_at"],effective_at=capture.occurred_at,
            source_snapshot=decoded.snapshot,origin_capture=capture,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id);outcome="applied"
    elif capture.operation_type=="customer_creation":
        from customers.services.client_services import _persist_client
        business,_=_persist_client(shop=shop,actor=actor,installation=live.installation,data=decoded.request,
            canonical=decoded.canonical_request,source_snapshot=decoded.snapshot,origin_capture=capture,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id)
        outcome="applied"
    elif capture.operation_type=="customer_opening_debt":
        from customers.services.debt_services import _persist_open_debt
        business,_=_persist_open_debt(shop=shop,customer=customer,actor=actor,installation=live.installation,data=decoded.request,
            canonical=decoded.canonical_request,deadline=decoded.snapshot["due_at"],source_snapshot=decoded.snapshot,
            origin_capture=capture,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id)
        outcome="applied"
    elif capture.operation_type=="customer_collection":
        from customers.services.payment_services import _persist_customer_payment
        business,_=_persist_customer_payment(shop=shop,customer=customer,actor=actor,installation=live.installation,
            data=decoded.request,canonical=decoded.canonical_request,effective_at=capture.occurred_at,
            allocation_caps=[row["applied_amount_fcfa"] for row in decoded.snapshot["allocations"]],
            origin_capture=capture,recovery_review=request,source_snapshot=decoded.snapshot)
        known=Activity.objects.get(id=capture.operation_id)
        outcome="applied"
    else:
        products=Product.objects.filter(shop=shop,id__in=quantities).in_bulk()
        packs=ProductPack.objects.filter(product__shop=shop,id__in=[line["pack_id"] for line in decoded.snapshot["lines"] if line["pack_id"]]).in_bulk()
        snapshots=[]
        for line in decoded.snapshot["lines"]:
            snapshots.append({key:line[key] for key in ("id","product_version","product_name","unit_kind","unit_label",
                "quantity_milli","factor_milli","base_quantity_milli","unit_price_fcfa")} | {
                "product":products[line["product_id"]],"pack":packs.get(line["pack_id"])})
        remaining=[int(row["validated_after_milli"]) for row in preview["stocks"]]
        business,_=_persist_sale(shop=shop,actor=actor,installation=live.installation,data=decoded.request,
            canonical=decoded.canonical_request,snapshots=snapshots,basket=decoded.basket,customer=customer,
            due_at=decoded.snapshot["due_at"],remaining=remaining,quantities=quantities,effective_at=capture.occurred_at,
            origin_capture=capture,recovery_review=request)
        known=Activity.objects.get(id=capture.operation_id)
        outcome="applied"
    decision=RecoveryIntegrationDecision.objects.create(capture=capture,business_operation=known,
        operation_id=request["operation_id"],reviewed_by=actor,reviewed_installation=live.installation,
        previous_version=request["previous_version"],version=request["previous_version"]+1,
        outcome=outcome,reason=request["reason"],source_sha256=capture.content_sha256,preview_sha256=request["preview_token"],
        request=request,receipt={})
    complete_sale_hold(hold=hold,capture=capture,actor=actor,installation=live.installation,request=request,integration=decision)
    if collection_review is not None:
        from backups.services.recovery_collection_reviews import complete_collection_review
        complete_collection_review(review=collection_review,capture=capture,actor=actor,installation=live.installation,
            request=request,integration=decision)
    receipt=json_value({"id":decision.id,"operation_id":decision.operation_id,"capture_id":capture.id,"version":decision.version,
        "business_operation_id":known.id,"outcome":outcome,"reviewed_by":actor.id,"reviewed_installation_id":live.installation_id,
        "reviewed_at":decision.created_at,"reason":decision.reason,"business_receipt":business})
    if capture.operation_type=='sale_cancellation':receipt['related_integration_decision_ids']=related_decisions
    decision.receipt=receipt
    decision.save(update_fields=["receipt","updated_at"])
    return receipt,True
