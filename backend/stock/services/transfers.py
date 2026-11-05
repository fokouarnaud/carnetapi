"""Current responsible roles confirm physical facts; receipt UUIDs prevent replay effects."""
import uuid

from django.db import transaction
from django.db.models import F, OuterRef, Subquery, Exists, Prefetch, Q, Value, CharField, Func
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from rest_framework import serializers

from catalogue.models import Product
from common.models import Activity
from common.services.operations import original_receipt, json_value
from common.services.errors import OperationConflict
from identity.models import Shop
from purchasing.api.suppliers import supplier_shop
from stock.models import StockMovement, StockTransfer, StockTransferLine, StockTransferReceipt, StockTransferReceiptLine, StockCostSnapshot
from stock.services.movement_costs import freeze_operation_costs
from stock.services.moving_average_cost import returned_cost_nanos
from stock.services.stock_alert_services import observe_stock
from stock.services.stock_queries import StockTotal, with_stock


def transfer_lines():
    received=StockTransferReceiptLine.objects.filter(transfer_line_id=OuterRef("pk")).order_by().values("transfer_line_id").annotate(
        total=StockTotal("quantity_milli")).values("total")
    revised=StockCostSnapshot.objects.filter(movement_id=OuterRef("movement_id"),version__gt=1)
    return StockTransferLine.objects.select_related("source_cost__movement").annotate(
        received_milli=Coalesce(Subquery(received),Value("0"),output_field=CharField()),
        source_cost_revision_pending=Exists(revised)).annotate(in_transit_milli=Func(F("quantity_milli"),F("received_milli"),
            function="stock_integer_subtract",output_field=CharField()))


def transfers_for(shop):
    return StockTransfer.objects.filter(Q(source_shop=shop)|Q(destination_shop=shop)).select_related("operation").prefetch_related(
        Prefetch("lines",queryset=transfer_lines())).order_by("occurred_at","id")


def current_transfer(shop,identity):
    return get_object_or_404(transfers_for(shop),id=identity)


def _unit(product,quantity):
    if product.unit_kind=="piece" and quantity%1000:
        raise serializers.ValidationError({"code":"transfer_piece_requires_whole_quantity"})


def _event(shop,request,data,action,target):
    from backups.models import RecoveryJournalOperation, RecoveryIntegrationDecision
    if (RecoveryJournalOperation.objects.filter(operation_id=data["operation_id"]).exists()
            or RecoveryIntegrationDecision.objects.filter(operation_id=data["operation_id"]).exists()):
        raise OperationConflict()
    return Activity.objects.create(id=data["operation_id"],shop=shop,actor=request.user,action=action,
        target_id=target,reason=data["reason"])


@transaction.atomic
def depart_transfer(shop_id,request,data):
    from stock.api.transfers import TransferSerializer
    shop=supplier_shop(shop_id,request)
    canonical=json_value({**data,"installation_id":request.device_session.installation_id})
    old=original_receipt(data["operation_id"],shop,request.user,"stock_transfer_departed",canonical)
    if old is not None:return old,False
    destination=get_object_or_404(Shop,id=data["destination_shop_id"],owner_id=shop.owner_id,is_active=True)
    if destination.id==shop.id:raise serializers.ValidationError({"code":"transfer_different_shops_required"})
    if not data["is_departure_confirmed"]:raise serializers.ValidationError({"code":"transfer_departure_confirmation_required"})
    if StockTransfer.objects.filter(id=data["id"]).exists() or StockTransferLine.objects.filter(id__in=[row["id"] for row in data["lines"]]).exists():
        raise OperationConflict()
    products={row.id:row for row in with_stock(Product.objects.filter(shop=shop,is_active=True,id__in=[row["product_id"] for row in data["lines"]]))}
    quantities={}
    for row in data["lines"]:
        product=products.get(row["product_id"])
        if product is None:get_object_or_404(Product.objects.none())
        if product.version!=row["product_version"]:raise OperationConflict()
        _unit(product,row["quantity_milli"])
        quantities[product.id]=quantities.get(product.id,0)+row["quantity_milli"]
    # The product contract does not yet decide dispatch exceeding known available
    # stock. Preserve all data while this case awaits its explicit business rule.
    for identity,quantity in quantities.items():
        if quantity>int(products[identity].stock_available_milli):
            raise serializers.ValidationError({"code":"transfer_departure_stock_review_required"})
    event=_event(shop,request,data,"stock_transfer_departed",data["id"])
    transfer=StockTransfer.objects.create(id=data["id"],source_shop=shop,destination_shop=destination,
        operation=event,installation=request.device_session.installation,occurred_at=data["occurred_at"])
    movements=[]
    for row in data["lines"]:
        product=products[row["product_id"]]
        movement=StockMovement.objects.create(product=product,actor=request.user,operation=event,kind="transfer_out",
            quantity_milli=-row["quantity_milli"],occurred_at=data["occurred_at"],reason=data["reason"])
        movements.append((row,product,movement))
    freeze_operation_costs(event,quantities,data["occurred_at"],kind="transfer_out")
    for row,product,movement in movements:
        StockTransferLine.objects.create(id=row["id"],transfer=transfer,product=product,product_version=product.version,
            product_name=product.name,unit_kind=product.unit_kind,unit_label=product.unit_label,
            quantity_milli=row["quantity_milli"],movement=movement,source_cost=movement.cost_snapshots.get(version=1))
    observe_stock(quantities,event)
    response=json_value(TransferSerializer(current_transfer(shop,transfer.id)).data)
    event.details={"request":canonical,"response":response};event.save(update_fields=["details","updated_at"])
    return response,True


@transaction.atomic
def receive_transfer(shop_id,identity,request,data):
    shop=supplier_shop(shop_id,request)
    transfer=get_object_or_404(StockTransfer,id=identity,destination_shop=shop,source_shop__owner_id=shop.owner_id)
    canonical=json_value({**data,"transfer_id":transfer.id,"installation_id":request.device_session.installation_id})
    old=original_receipt(data["operation_id"],shop,request.user,"stock_transfer_received",canonical)
    if old is not None:return old,False
    if not data["is_sellable_confirmed"]:raise serializers.ValidationError({"code":"transfer_sellable_confirmation_required"})
    if data["occurred_at"]<transfer.occurred_at:raise serializers.ValidationError({"code":"transfer_receipt_precedes_departure"})
    if StockTransferReceipt.objects.filter(id=data["id"]).exists() or StockTransferReceiptLine.objects.filter(id__in=[row["id"] for row in data["lines"]]).exists():
        raise OperationConflict()
    lines=transfer_lines().filter(transfer=transfer).in_bulk()
    products=Product.objects.filter(shop=shop,is_active=True,id__in=[row["product_id"] for row in data["lines"]]).in_bulk()
    before={identity:int(line.received_milli) for identity,line in lines.items()}
    prepared=[]
    for row in data["lines"]:
        line=lines.get(row["transfer_line_id"]);product=products.get(row["product_id"])
        if line is None or product is None:get_object_or_404(Product.objects.none())
        if (line.unit_kind,line.unit_label)!=(product.unit_kind,product.unit_label):
            raise serializers.ValidationError({"code":"transfer_unit_mismatch"})
        _unit(product,row["quantity_milli"])
        previous=before[line.id]
        if previous+row["quantity_milli"]>line.quantity_milli:raise OperationConflict()
        allocated=returned_cost_nanos(line.source_cost.total_cost_nanos_fcfa,line.quantity_milli,previous,row["quantity_milli"])
        prepared.append((row,line,product,previous,allocated))
        before[line.id]+=row["quantity_milli"]
    return _persist_transfer_receipt(shop=shop,transfer=transfer,actor=request.user,
        installation=request.device_session.installation,data=data,canonical=canonical,prepared=prepared)


def _persist_transfer_receipt(*,shop,transfer,actor,installation,data,canonical,prepared,
        origin_capture=None,source_snapshot=None,recovery_review=None):
    """Persist validated native or recovered facts in the caller's transaction."""
    from stock.api.transfers import TransferReceiptSerializer
    event=Activity.objects.create(id=data["operation_id"],shop=shop,actor=actor,
        action="stock_transfer_received",target_id=data["id"],reason=data["reason"],details={"request":canonical})
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=event,capture=origin_capture)
        event.details["recovery"]=json_value({"capture_id":origin_capture.id,
            "origin_user_id":origin_capture.origin_user_id,"origin_installation_id":origin_capture.origin_installation_id,
            "origin_recorded_at":origin_capture.origin_recorded_at,"origin_installation_verified":False,
            "source_snapshot":source_snapshot,"review":recovery_review})
    receipt=StockTransferReceipt.objects.create(id=data["id"],transfer=transfer,operation=event,
        installation=installation,occurred_at=data["occurred_at"])
    source_rows={row["id"]:row for row in source_snapshot["lines"]} if source_snapshot else {}
    if source_snapshot:
        # Before marks were checked in request order. A native reply can use a
        # different SQL row order; preserve that reply without pairing by index.
        by_id={row[0]["id"]:row for row in prepared}
        prepared=[by_id[row["id"]] for row in source_snapshot["lines"]]
    for row,line,product,previous,allocated in prepared:
        source=source_rows.get(row["id"])
        # The native database generated this identity once; replay keeps it.
        movement=StockMovement.objects.create(id=source["movement_id"] if source else uuid.uuid4(),
            product=product,actor=actor,operation=event,kind="transfer_in",quantity_milli=row["quantity_milli"],
            occurred_at=data["occurred_at"],reason=data["reason"])
        StockTransferReceiptLine.objects.create(id=row["id"],receipt=receipt,transfer_line=line,product=product,
            product_version=source["product_version"] if source else product.version,
            product_name=source["product_name"] if source else product.name,
            unit_kind=source["unit_kind"] if source else product.unit_kind,
            unit_label=source["unit_label"] if source else product.unit_label,
            quantity_milli=row["quantity_milli"],received_before_milli=previous,
            cost_state=source["cost_state"] if source else line.source_cost.cost_state,
            total_cost_nanos_fcfa=allocated,movement=movement)
    product_ids={row[2].id for row in prepared}
    freeze_operation_costs(event,product_ids,data["occurred_at"],kind="transfer_in")
    observe_stock(product_ids,event)
    response=json_value(TransferReceiptSerializer(StockTransferReceipt.objects.select_related("operation").prefetch_related("lines").get(id=receipt.id)).data)
    event.details["response"]=response;event.save(update_fields=["details","updated_at"])
    return response,True
