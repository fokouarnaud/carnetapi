"""Reserve recovered physical departures apart from validated money and sales."""
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from backups.models import RecoverySaleHold,RecoverySaleHoldDecision,RecoveryIntegrationDecision,RestoreAccessGeneration,RecoveryClosure
from common.api.fields import ExactIntegerField,StrictSerializer
from common.models import Activity
from common.services.errors import OperationConflict
from common.services.operations import json_value
from sales.services.pricing import MAX_FCFA
from stock.models import StockReservation
from stock.services.stock_alert_services import observe_stock


class RecoverySaleHoldInput(StrictSerializer):
    operation_id=serializers.UUIDField()
    previous_version=ExactIntegerField(min_value=0,max_value=MAX_FCFA-1)
    preview_token=serializers.RegexField(r"^[0-9a-f]{64}$")
    state=serializers.ChoiceField(choices=["pending","refused"])
    reason=serializers.CharField(max_length=2000,allow_blank=False)


class RecoverySaleReviewDenied(PermissionDenied):
    default_detail=_("Only a current shop responsible can review recovered sales.")


def hold_context(shop_id,actor,session_id):
    from backups.services.recovery_integration import integration_context,RecoveryIntegrationDenied
    try:return integration_context(shop_id,actor,session_id)
    except RecoveryIntegrationDenied:raise RecoverySaleReviewDenied() from None


def find_sale_hold(capture,quantities=None):
    hold=RecoverySaleHold.objects.filter(source_operation_id=capture.operation_id,
        capture__shop_id=capture.shop_id).select_related("capture").defer("capture__payload","capture__dependencies").first()
    if hold is None:
        if RecoverySaleHold.objects.filter(source_operation_id=capture.operation_id).exists():raise OperationConflict()
        return None
    if hold.capture.content_sha256!=capture.content_sha256 or hold.capture.operation_id!=hold.source_operation_id:
        raise OperationConflict()
    latest=hold.decisions.order_by("-version").values("version","state").first()
    if latest!={"version":hold.version,"state":hold.state}:raise OperationConflict()
    if quantities is not None:
        expected_state={"pending":"pending","refused":"rejected","validated":"validated"}[hold.state]
        rows=list(StockReservation.objects.filter(operation_id=hold.reservation_operation_id)
            .values("product_id","product__shop_id","quantity_milli","state"))
        if ({row["product_id"]:row["quantity_milli"] for row in rows}!=quantities
                or any(row["state"]!=expected_state or row["product__shop_id"]!=capture.shop_id for row in rows)):
            raise OperationConflict()
    return hold


def money_facts(net,paid):
    return {"net_total_fcfa":net,"payments_fcfa":paid,"unpaid_debt_fcfa":max(0,net-paid),"customer_credit_fcfa":max(0,paid-net)}


def review_event(capture,actor,request):
    if request["operation_id"]==str(capture.operation_id) or Activity.objects.filter(id=request["operation_id"]).exists():
        raise OperationConflict()
    return Activity.objects.create(id=request["operation_id"],shop_id=capture.shop_id,actor=actor,
        action="recovery_sale_"+request["state"],target_id=capture.payload["sale"]["id"],reason=request["reason"],
        details={"request":request,"source":json_value({"capture_id":capture.id,"operation_id":capture.operation_id,
            "origin_user_id":capture.origin_user_id,"origin_installation_id":capture.origin_installation_id,
            "source_sha256":capture.content_sha256})})


def record_hold_decision(*,hold,capture,actor,installation,request,state,event,integration=None):
    decision=RecoverySaleHoldDecision.objects.create(hold=hold,capture=capture,operation_id=request["operation_id"],
        reviewed_by=actor,reviewed_installation=installation,previous_version=hold.version,version=hold.version+1,
        state=state,reason=request["reason"],request=request,receipt={},integration_decision=integration)
    hold.state=state
    hold.version=decision.version
    hold.save(update_fields=["state","version","updated_at"])
    reserved=list(StockReservation.objects.filter(operation=hold.reservation_operation).order_by("product_id")
        .values("id","product_id","quantity_milli","state"))
    receipt=json_value({"id":decision.id,"hold_id":hold.id,"capture_id":capture.id,"operation_id":decision.operation_id,
        "source_operation_id":hold.source_operation_id,"source_sale_id":hold.sale_id,"state":state,"version":decision.version,"reason":decision.reason,
        "reviewed_by":actor.id,"reviewed_installation_id":installation.id,"reviewed_at":decision.created_at,
        "integration_decision_id":integration.id if integration else None,"reservations":reserved,
        **money_facts(hold.net_total_fcfa,hold.payments_fcfa)})
    decision.receipt=receipt
    decision.save(update_fields=["receipt","updated_at"])
    event.details["response"]=receipt
    event.save(update_fields=["details","updated_at"])
    return decision


def complete_sale_hold(*,hold,capture,actor,installation,request,integration):
    if hold is None or hold.state!="pending":return None
    event=review_event(capture,actor,{**request,"state":"validated"})
    decision=record_hold_decision(hold=hold,capture=capture,actor=actor,installation=installation,
        request=request,state="validated",event=event,integration=integration)
    observe_stock(StockReservation.objects.filter(operation=hold.reservation_operation).values_list("product_id",flat=True),event)
    return decision


@transaction.atomic
def decide_sale_hold(*,shop_id,actor,session_id,capture_id,data):
    from backups.services.recovery_integration import _capture,_build_preview,RecoveryTypeUnsupported
    shop,live=hold_context(shop_id,actor,session_id)
    if not RestoreAccessGeneration.objects.filter(current_slot=1).update(current_slot=1):raise OperationConflict()
    shop,live=hold_context(shop_id,actor,session_id)
    capture=_capture(shop,capture_id)
    if capture.operation_type!="sale":raise RecoveryTypeUnsupported()
    incoming=RecoverySaleHoldInput(data=data)
    incoming.is_valid(raise_exception=True)
    request=json_value(incoming.validated_data)
    previous=RecoverySaleHoldDecision.objects.filter(operation_id=request["operation_id"]).first()
    if previous is not None:
        if (previous.capture_id!=capture.id or previous.reviewed_by_id!=actor.id
                or previous.reviewed_installation_id!=live.installation_id or previous.request!=request):raise OperationConflict()
        return previous.receipt,False
    if (RecoveryIntegrationDecision.objects.filter(operation_id=request["operation_id"]).exists()
            or RecoveryClosure.objects.filter(operation_id=request["operation_id"]).exists()):raise OperationConflict()
    preview,decoded,known,quantities,_customer=_build_preview(shop,capture)
    hold=find_sale_hold(capture,quantities)
    allowed=preview["can_hold"] if request["state"]=="pending" else preview["can_refuse"]
    if not allowed or request["preview_token"]!=preview["preview_token"] or request["previous_version"]!=preview["review_version"]:
        raise OperationConflict()
    event=review_event(capture,actor,request)
    if hold is None:
        paid=sum(payment["amount_fcfa"] for payment in decoded.snapshot["payments"])
        hold=RecoverySaleHold.objects.create(capture=capture,source_operation_id=capture.operation_id,sale_id=decoded.snapshot["id"],
            reservation_operation=event,net_total_fcfa=decoded.basket.net_total_fcfa,payments_fcfa=paid)
        StockReservation.objects.bulk_create([StockReservation(product_id=product_id,operation=event,quantity_milli=quantity)
            for product_id,quantity in quantities.items()])
    if request["state"]=="refused":
        StockReservation.objects.filter(operation=hold.reservation_operation).update(state="rejected")
    decision=record_hold_decision(hold=hold,capture=capture,actor=actor,installation=live.installation,
        request=request,state=request["state"],event=event)
    observe_stock(quantities,event)
    return decision.receipt,True
