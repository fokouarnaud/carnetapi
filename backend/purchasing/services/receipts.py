"""Append a physical purchase arrival and retain the source of its exact cost."""
import uuid

from django.db.models import OuterRef, Subquery

from common.models import Activity
from common.services.operations import json_value
from common.services.origins import OriginReferenceConflict
from purchasing.models import PurchaseChargeAllocation, PurchaseChargeRevision, PurchaseReceipt, PurchaseReceiptLine
from stock.models import StockMovement
from stock.services.moving_average_cost import receipt_cost_nanos
from stock.services.stock_alert_services import observe_stock


def current_receipt_charge_allocations(line):
    """Read source identities only after checking their document and shop linkage."""
    all_rows=PurchaseChargeAllocation.objects.filter(line=line)
    if all_rows.exclude(revision__charge__document_id=line.document_id,
            revision__operation__shop_id=line.document.shop_id,
            revision__charge__operation__shop_id=line.document.shop_id).exists():raise OriginReferenceConflict()
    latest=PurchaseChargeRevision.objects.filter(charge_id=OuterRef('revision__charge_id')).order_by('-version').values('version')[:1]
    allocations=all_rows.filter(revision__version=Subquery(latest)).select_related('revision__charge').order_by('revision__charge_id','id')
    return json_value([{'allocation_id':row.id,'revision_id':row.revision_id,'charge_id':row.revision.charge_id,
           'amount_fcfa':row.amount_fcfa} for row in allocations])


def purchase_receipt_cost_basis(line, base_quantity, received_before_base):
    """Preserve actual revision/allocation identities; absent history is never inferred."""
    rows=current_receipt_charge_allocations(line)
    total=line.total_fcfa+sum(row['amount_fcfa'] for row in rows)
    return json_value({'invoice_total_fcfa':line.total_fcfa,'invoice_base_quantity_milli':line.base_quantity_milli,
        'received_before_base_milli':received_before_base,'total_cost_nanos_fcfa':str(receipt_cost_nanos(total,line.base_quantity_milli,received_before_base,base_quantity)),
        'charge_allocations':rows})


def _persist_purchase_receipt(*,shop,document,actor,installation,data,canonical,prepared,
        origin_capture=None,source_snapshot=None,recovery_review=None):
    """Persist validated rows inside the caller's authorized, serialized transaction."""
    from purchasing.api.purchase_receipt_serializers import ReceiptSerializer
    event=Activity.objects.create(id=data['operation_id'],shop=shop,actor=actor,action='purchase_received',
        target_id=data['id'],reason=data['reason'],details={'request':canonical,'receipt_proofs':{}})
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=event,capture=origin_capture)
        event.details['recovery']=json_value({'capture_id':origin_capture.id,'origin_user_id':origin_capture.origin_user_id,
            'origin_installation_id':origin_capture.origin_installation_id,'origin_recorded_at':origin_capture.origin_recorded_at,
            'origin_installation_verified':False,'source_snapshot':source_snapshot,'review':recovery_review})
    receipt=PurchaseReceipt.objects.create(id=data['id'],document=document,operation=event,installation=installation,occurred_at=data['occurred_at'])
    source_rows={row['line_id']:row for row in source_snapshot['lines']} if source_snapshot else {}
    for line,quantity,base in prepared:
        source=source_rows.get(line.id)
        before=sum(line.receipt_lines.values_list('base_quantity_milli',flat=True))
        basis=source['cost_basis'] if source else purchase_receipt_cost_basis(line,base,before)
        row_id=source['id'] if source else uuid.uuid5(data['id'],'purchase-receipt-line:'+str(line.id))
        movement_id=source['movement_id'] if source else uuid.uuid5(data['id'],'purchase-stock:'+str(line.id))
        event.details['receipt_proofs'][str(row_id)]=json_value({'unit_kind':source['unit_kind'] if source else line.product.unit_kind,'cost_basis':basis})
        movement=StockMovement.objects.create(id=movement_id,product=line.product,actor=actor,operation=event,
            kind='purchase_receipt',quantity_milli=base,occurred_at=data['occurred_at'],reason=data['reason'])
        PurchaseReceiptLine.objects.create(id=row_id,receipt=receipt,line=line,movement=movement,quantity_milli=quantity,base_quantity_milli=base)
    event.save(update_fields=['details','updated_at'])
    result=json_value(ReceiptSerializer(receipt).data)
    event.details['response']=result;event.save(update_fields=['details','updated_at'])
    observe_stock([line.product_id for line,_,_ in prepared],event)
    return result,True
