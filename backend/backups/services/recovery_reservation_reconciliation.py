"""Match refused reservations to validated physical departures, never post another departure."""
import hashlib
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from backups.journal_protocol import canonical_bytes
from backups.models import RestoreAccessGeneration, RecoveryIntegrationDecision, RecoveryJournalOperation, RecoveryClosure
from backups.services.recovery_integration import _capture
from backups.services.recovery_sale_holds import hold_context, find_sale_hold
from backups.services.recovery_sale_snapshot import decode_sale_snapshot
from common.api.fields import ExactIntegerField, StrictSerializer
from common.models import Activity
from common.services.errors import OperationConflict
from common.services.operations import json_value
from sales.services.pricing import MAX_FCFA
from stock.models import StockReservation, StockMovement, StockReservationReconciliation, StockLoss, StockCountAdjustment
from stock.services.stock_queries import StockTotal, with_stock
from stock.services.stock_alert_services import observe_stock
from catalogue.models import Product


class ReconciliationPreviewInput(StrictSerializer):
    movement_id=serializers.UUIDField()
    quantity_milli=ExactIntegerField(min_value=1,max_value=MAX_FCFA)


class ReconciliationInput(ReconciliationPreviewInput):
    operation_id=serializers.UUIDField()
    preview_token=serializers.RegexField(r"^[0-9a-f]{64}$")
    reason=serializers.CharField(max_length=2000,allow_blank=False)


def reconciliation_context(shop_id,actor,session_id,capture_id,reservation_id):
    shop,live=hold_context(shop_id,actor,session_id)
    capture=_capture(shop,capture_id)
    decoded=decode_sale_snapshot(capture,timezone_name=shop.timezone)
    quantities={}
    for line in decoded.snapshot["lines"]:
        quantities[line["product_id"]]=quantities.get(line["product_id"],0)+line["base_quantity_milli"]
    hold=find_sale_hold(capture,quantities)
    reservation=get_object_or_404(StockReservation, id=reservation_id, product__shop=shop,
        operation_id=hold.reservation_operation_id if hold else None)
    return shop,live,capture,hold,reservation


def total(rows):
    return int(rows.aggregate(total=StockTotal("quantity_milli"))["total"] or "0")


def movement_capacity(movement):
    """Inventory revisions share one physical observation, including positive revisions."""
    links=StockReservationReconciliation.objects.filter(movement=movement)
    accounted=max(0,-movement.quantity_milli)
    claimed=total(links)
    valid=False
    count_basis=None
    if movement.kind=="loss":
        valid=StockLoss.objects.filter(movement=movement,product_id=movement.product_id,operation_id=movement.operation_id,
            quantity_milli=accounted,operation__action="stock_loss_recorded",operation__target_id=F("id")).exists()
    elif movement.kind=="inventory_adjustment":
        adjustment=StockCountAdjustment.objects.filter(movement=movement,count__product_id=movement.product_id,
            operation_id=movement.operation_id,delta_milli=movement.quantity_milli,
            operation__action="stock_count_adjusted").first()
        if adjustment is not None and movement.operation.target_id==adjustment.count_id:
            valid=True
            net=int(StockCountAdjustment.objects.filter(count_id=adjustment.count_id)
                .aggregate(total=StockTotal("delta_milli"))["total"] or "0")
            group_claimed=total(StockReservationReconciliation.objects.filter(movement__count_adjustment__count_id=adjustment.count_id))
            count_basis={"count_id":str(adjustment.count_id),"net_adjustment_milli":str(net),"claimed_milli":str(group_claimed)}
            remaining=max(0,min(accounted-claimed,max(0,-net)-group_claimed))
            return valid and movement.quantity_milli<0,accounted,claimed,remaining,count_basis
    return valid and movement.quantity_milli<0,accounted,claimed,max(0,accounted-claimed),count_basis


def build_preview(shop,capture,hold,reservation,data):
    movement=get_object_or_404(StockMovement.objects.select_related("operation"), id=data["movement_id"],
        product_id=reservation.product_id,product__shop=shop,operation__shop=shop)
    valid,accounted,claimed,capacity,count_basis=movement_capacity(movement)
    released=total(reservation.reconciliations.all())
    remaining=reservation.quantity_milli-released
    quantity=data["quantity_milli"]
    product=with_stock(Product.objects.filter(id=reservation.product_id,shop=shop)).get()
    issues=[]
    if hold.state!="refused" or reservation.state!="rejected":issues.append("reservation_not_refused")
    if not valid:issues.append("movement_not_validated_negative_loss_or_inventory")
    if quantity>remaining:issues.append("reservation_quantity_exceeded")
    if quantity>capacity:issues.append("accounted_quantity_exceeded")
    if product.unit_kind=="piece" and quantity%1000:issues.append("piece_requires_whole_quantity")
    result=json_value({"capture_id":capture.id,"hold_id":hold.id,"reservation_id":reservation.id,
        "source_operation_id":hold.source_operation_id,"product_id":product.id,"movement_id":movement.id,
        "quantity_milli":quantity,"reservation_original_milli":str(reservation.quantity_milli),
        "reservation_reconciled_milli":str(released),"reservation_remaining_milli":str(remaining),
        "movement_accounted_milli":str(accounted),"movement_claimed_milli":str(claimed),"movement_remaining_milli":str(capacity),
        "validated_before_milli":product.stock_validated_milli,"validated_after_milli":product.stock_validated_milli,
        "reserved_before_milli":product.stock_reserved_milli,"reserved_after_milli":str(int(product.stock_reserved_milli)-quantity),
        "available_before_milli":product.stock_available_milli,"available_after_milli":str(int(product.stock_available_milli)+quantity),
        "can_apply":not issues,"issues":issues})
    basis={"preview":result,"source_sha256":capture.content_sha256,"review_version":hold.version,
        "movement_quantity":movement.quantity_milli,"movement_updated_at":json_value(movement.updated_at),
        "unit_kind":product.unit_kind,"count":count_basis}
    result["preview_token"]=hashlib.sha256(canonical_bytes(basis)).hexdigest()
    return result,movement


@transaction.atomic
def preview_reconciliation(*,shop_id,actor,session_id,capture_id,reservation_id,data):
    shop,_live,capture,hold,reservation=reconciliation_context(shop_id,actor,session_id,capture_id,reservation_id)
    incoming=ReconciliationPreviewInput(data=data);incoming.is_valid(raise_exception=True)
    return build_preview(shop,capture,hold,reservation,incoming.validated_data)[0]


@transaction.atomic
def reconcile_reservation(*,shop_id,actor,session_id,capture_id,reservation_id,data):
    hold_context(shop_id,actor,session_id)
    if not RestoreAccessGeneration.objects.filter(current_slot=1).update(current_slot=1):raise OperationConflict()
    shop,live,capture,hold,reservation=reconciliation_context(shop_id,actor,session_id,capture_id,reservation_id)
    incoming=ReconciliationInput(data=data);incoming.is_valid(raise_exception=True)
    request=json_value({**incoming.validated_data,"capture_id":capture.id,"reservation_id":reservation.id,"installation_id":live.installation_id})
    previous=StockReservationReconciliation.objects.filter(operation_id=request["operation_id"],reservation=reservation).select_related("operation").first()
    if previous is not None:
        if previous.operation.actor_id!=actor.id or previous.operation.details.get("request")!=request:raise OperationConflict()
        return previous.operation.details["response"],False
    if (Activity.objects.filter(id=request["operation_id"]).exists()
            or RecoveryClosure.objects.filter(operation_id=request["operation_id"]).exists()
            or RecoveryIntegrationDecision.objects.filter(operation_id=request["operation_id"]).exists()
            or RecoveryJournalOperation.objects.filter(operation_id=request["operation_id"]).exists()):raise OperationConflict()
    preview,movement=build_preview(shop,capture,hold,reservation,incoming.validated_data)
    if not preview["can_apply"] or preview["preview_token"]!=request["preview_token"]:raise OperationConflict()
    event=Activity.objects.create(id=request["operation_id"],shop=shop,actor=actor,action="recovery_reservation_reconciled",
        target_id=reservation.id,reason=request["reason"],details={"request":request,"before_after":preview,
            "origin_user_id":str(capture.origin_user_id),"origin_installation_id":str(capture.origin_installation_id)})
    link=StockReservationReconciliation.objects.create(reservation=reservation,movement=movement,operation=event,
        installation=live.installation,quantity_milli=request["quantity_milli"])
    receipt=json_value({"id":link.id,"operation_id":event.id,"capture_id":capture.id,"reservation_id":reservation.id,
        "movement_id":movement.id,"product_id":reservation.product_id,"quantity_milli":link.quantity_milli,
        "source_operation_id":hold.source_operation_id,"actor_id":actor.id,"installation_id":live.installation_id,
        "recorded_at":event.created_at,"reason":event.reason,"before_after":preview})
    event.details["response"]=receipt;event.save(update_fields=["details","updated_at"])
    observe_stock([reservation.product_id],event)
    return receipt,True
