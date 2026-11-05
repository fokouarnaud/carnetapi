"""Review lost physical purchase arrivals using retained source identities and costs."""
from dataclasses import dataclass
import hashlib
import uuid

from django.db.models import F,Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import APIException,ValidationError

from backups.journal_protocol import JournalDateTimeField,canonical_bytes
from backups.services.recovery_customers import recorded_origin_evidence
from backups.services.recovery_return_receipts import CostIntegerString
from catalogue.models import Product
from common.api.fields import ExactIntegerField,StrictSerializer
from common.models import Activity
from common.services.operations import json_value
from common.services.origins import OriginReferenceConflict
from identity.models import Installation
from purchasing.models import PurchaseDocument,PurchaseLine,PurchaseReceipt,PurchaseReceiptLine,PurchaseChargeAllocation
from purchasing.services.receipts import current_receipt_charge_allocations
from sales.services.pricing import MAX_FCFA
from stock.models import StockMovement
from stock.services.moving_average_cost import receipt_cost_nanos
from stock.services.stock_queries import with_stock


class RecoveryPurchaseReceiptInvalid(APIException):
    status_code=400
    default_code='recovery_purchase_receipt_invalid'
    default_detail=_("Recovery physical purchase receipt snapshot is inconsistent.")


class FrozenReceiptChargeAllocation(StrictSerializer):
    allocation_id=serializers.UUIDField()
    revision_id=serializers.UUIDField()
    charge_id=serializers.UUIDField()
    amount_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)


class FrozenReceiptCostBasis(StrictSerializer):
    invoice_total_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    invoice_base_quantity_milli=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    received_before_base_milli=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    total_cost_nanos_fcfa=CostIntegerString()
    charge_allocations=FrozenReceiptChargeAllocation(many=True,max_length=500)


class FrozenPurchaseReceiptLine(StrictSerializer):
    id=serializers.UUIDField(required=False,allow_null=True)
    movement_id=serializers.UUIDField(required=False,allow_null=True)
    line_id=serializers.UUIDField()
    product_id=serializers.UUIDField(required=False,allow_null=True)
    unit_kind=serializers.ChoiceField(choices=Product.UnitKind.choices,required=False,allow_null=True)
    quantity_milli=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    base_quantity_milli=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    cost_basis=FrozenReceiptCostBasis(required=False,allow_null=True)


class FrozenPurchaseReceipt(StrictSerializer):
    id=serializers.UUIDField()
    document_id=serializers.UUIDField()
    occurred_at=JournalDateTimeField()
    created_at=JournalDateTimeField()
    lines=FrozenPurchaseReceiptLine(many=True,allow_empty=False,max_length=500)


class PurchaseReceiptPayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    receipt=FrozenPurchaseReceipt()


@dataclass(frozen=True)
class DecodedPurchaseReceipt:
    request:dict
    snapshot:dict
    canonical_request:dict
    has_proof:bool


def decode_purchase_receipt(capture):
    """Accept old receipts for review while refusing to manufacture missing proof."""
    from purchasing.api.purchases import ReceiptInput
    class SourceReceiptInput(ReceiptInput):
        occurred_at=JournalDateTimeField()
        def validate_occurred_at(self,value):return value
    try:
        incoming=PurchaseReceiptPayload(data=capture.payload);incoming.is_valid(raise_exception=True)
        snapshot=incoming.validated_data['receipt'];raw=incoming.validated_data['original_request']
        if not isinstance(raw,dict):raise RecoveryPurchaseReceiptInvalid()
        raw=dict(raw)
        if (uuid.UUID(str(raw.pop('document_id',snapshot['document_id'])))!=snapshot['document_id']
                or uuid.UUID(str(raw.pop('installation_id',capture.origin_installation_id)))!=capture.origin_installation_id):raise RecoveryPurchaseReceiptInvalid()
        request=SourceReceiptInput(data=raw);request.is_valid(raise_exception=True);data=request.validated_data
        if (data['id']!=snapshot['id'] or data['operation_id']!=capture.operation_id
                or data['occurred_at']!=capture.occurred_at or snapshot['occurred_at']!=capture.occurred_at
                or snapshot['created_at']<snapshot['occurred_at'] or len(data['lines'])!=len(snapshot['lines'])):raise RecoveryPurchaseReceiptInvalid()
        asked_by_line={row['line_id']:row for row in data['lines']}
        frozen_line_ids={row['line_id'] for row in snapshot['lines']}
        if len(frozen_line_ids)!=len(snapshot['lines']) or frozen_line_ids!=set(asked_by_line):raise RecoveryPurchaseReceiptInvalid()
        has_proof=True;ids=set();movements=set()
        # Native rows are ordered by persistence time/UUID, independently of
        # request order. Preserve both orders and compare their unique identities.
        for row in snapshot['lines']:
            asked=asked_by_line[row['line_id']]
            if any(asked[key]!=row[key] for key in ('line_id','quantity_milli')):raise RecoveryPurchaseReceiptInvalid()
            if not all(row.get(key) is not None for key in ('id','movement_id','product_id','unit_kind','cost_basis')):
                has_proof=False;continue
            if row['id'] in ids or row['movement_id'] in movements:raise RecoveryPurchaseReceiptInvalid()
            ids.add(row['id']);movements.add(row['movement_id'])
            basis=row['cost_basis'];allocations=basis['charge_allocations']
            if (len({part['allocation_id'] for part in allocations})!=len(allocations)
                    or len({part['charge_id'] for part in allocations})!=len(allocations)
                    or row['unit_kind']=='piece' and row['base_quantity_milli']%1000):raise RecoveryPurchaseReceiptInvalid()
            total=basis['invoice_total_fcfa']+sum(part['amount_fcfa'] for part in allocations)
            if int(basis['total_cost_nanos_fcfa'])!=receipt_cost_nanos(total,basis['invoice_base_quantity_milli'],basis['received_before_base_milli'],row['base_quantity_milli']):raise RecoveryPurchaseReceiptInvalid()
        return DecodedPurchaseReceipt(data,snapshot,json_value({**data,'document_id':snapshot['document_id'],
            'installation_id':capture.origin_installation_id}),has_proof)
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):raise RecoveryPurchaseReceiptInvalid() from None


def _historical_allocation_matches(shop,document,line,allocation):
    return PurchaseChargeAllocation.objects.filter(id=allocation['allocation_id'],line=line,revision_id=allocation['revision_id'],
        revision__charge_id=allocation['charge_id'],revision__charge__document=document,
        revision__charge__operation__shop=shop,revision__operation__shop=shop,amount_fcfa=allocation['amount_fcfa']).exists()


def _known_receipt(shop,capture,decoded,document,issues,notices):
    operation=Activity.objects.filter(shop=shop,id=capture.operation_id).first()
    if operation is None:
        if Activity.objects.filter(id=capture.operation_id).exists():issues.append({'code':'operation_conflict','ids':[str(capture.operation_id)]})
        return None,False
    snapshot=decoded.snapshot;verified=False
    try:
        actor,device,verified,origin=recorded_origin_evidence(capture,operation,notices,client_metadata=False)
        result=operation.details['response']
        initial=operation.details['recovery']['source_snapshot'] if origin else result
        stored=PurchaseReceipt.objects.filter(id=snapshot['id'],document=document,operation=operation).first() if document else None
        matches=(stored is not None and actor==capture.origin_user_id and device and initial==json_value(snapshot)
            and operation.action=='purchase_received' and operation.target_id==snapshot['id'] and operation.reason==decoded.request['reason']
            and operation.details.get('request')==decoded.canonical_request and stored.occurred_at==capture.occurred_at
            and stored.installation.user_id==operation.actor_id
            and result['created_at']==json_value(stored.created_at) and result['document_id']==str(document.id)
            and result['id']==str(stored.id) and result['occurred_at']==json_value(capture.occurred_at)
            and not operation.cash_movements.exists() and not operation.supplier_ledger.exists())
        expected_rows={row['line_id']:row for row in snapshot['lines']}
        if matches:
            rows=list(stored.lines.select_related('line__product','movement'))
            matches=len(rows)==len(expected_rows) and operation.stock_movements.count()==len(rows)
            for row in rows:
                frozen=expected_rows.get(row.line_id)
                if frozen is None:matches=False;break
                matches=(matches and row.line.document_id==document.id and row.line.product.shop_id==shop.id
                    and row.quantity_milli==frozen['quantity_milli'] and row.base_quantity_milli==frozen['base_quantity_milli']
                    and row.movement.operation_id==operation.id and row.movement.product_id==row.line.product_id
                    and row.movement.actor_id==operation.actor_id
                    and row.movement.quantity_milli==row.base_quantity_milli and row.movement.kind=='purchase_receipt'
                    and row.movement.occurred_at==capture.occurred_at and row.movement.reason==decoded.request['reason'])
                if decoded.has_proof:
                    matches=(matches and row.id==frozen['id'] and row.movement_id==frozen['movement_id']
                        and row.line.product_id==frozen['product_id'] and operation.details['receipt_proofs'].get(str(row.id))==
                        json_value({'unit_kind':frozen['unit_kind'],'cost_basis':frozen['cost_basis']}))
                    basis=frozen['cost_basis']
                    matches=(matches and row.line.total_fcfa==basis['invoice_total_fcfa']
                        and row.line.base_quantity_milli==basis['invoice_base_quantity_milli']
                        and row.quantity_milli*row.line.factor_milli==row.base_quantity_milli*1000
                        and all(_historical_allocation_matches(shop,document,row.line,allocation) for allocation in basis['charge_allocations']))
            # Frozen response and metadata must agree even after costs are revised.
            from purchasing.api.purchase_receipt_serializers import ReceiptSerializer
            if decoded.has_proof:matches=matches and result==json_value(ReceiptSerializer(stored).data)
    except (KeyError,ValueError,TypeError,AttributeError):matches=False
    if not matches:issues.append({'code':'operation_conflict','ids':[str(capture.operation_id)]})
    return operation,verified


def build_purchase_receipt_preview(shop,capture):
    decoded=decode_purchase_receipt(capture);snapshot=decoded.snapshot;issues=[];notices=[];prepared=[]
    document=PurchaseDocument.objects.filter(id=snapshot['document_id'],shop=shop,supplier__shop=shop,kind='invoice',operation__shop=shop).first()
    # An archived foreign key is not authority to load another product or pack.
    # Check identities before recognizing a receipt or preparing its movements.
    lines=PurchaseLine.objects.filter(document=document) if document else PurchaseLine.objects.none()
    compatible=Q(product__shop=shop)&(Q(pack__isnull=True)|Q(pack__product_id=F('product_id')))
    inconsistent=list(lines.exclude(compatible).values_list('id',flat=True))
    if inconsistent:
        issues.append({'code':'purchase_line_relation_conflict','ids':[str(value) for value in inconsistent]})
        known,verified,rows=None,False,{}
    else:
        known,verified=_known_receipt(shop,capture,decoded,document,issues,notices)
        rows={row.id:row for row in lines.filter(compatible).select_related('product')}
    state=[];deltas={}
    if known is None:
        if not decoded.has_proof:issues.append({'code':'legacy_purchase_receipt_without_proof','ids':[]})
        if document is None:issues.append({'code':'missing_purchase_invoice','ids':[str(snapshot['document_id'])]})
        if capture.occurred_at>timezone.now() or snapshot['created_at']>timezone.now():issues.append({'code':'future_effective_date','ids':[]})
        if document and capture.occurred_at<document.occurred_at:issues.append({'code':'receipt_precedes_invoice','ids':[]})
        ready={str(value) for value in Activity.objects.filter(shop=shop,id__in=capture.dependencies,details__has_key='response').values_list('id',flat=True)}
        missing=sorted(set(capture.dependencies)-ready)
        if missing:issues.append({'code':'missing_dependencies','ids':missing})
        if (PurchaseReceipt.objects.filter(id=snapshot['id']).exists()
                or PurchaseReceiptLine.objects.filter(id__in=[row['id'] for row in snapshot['lines'] if row.get('id')]).exists()
                or StockMovement.objects.filter(id__in=[row['movement_id'] for row in snapshot['lines'] if row.get('movement_id')]).exists()):issues.append({'code':'entity_uuid_conflict','ids':[]})
    for frozen in snapshot['lines']:
        line=rows.get(frozen['line_id'])
        if line is None:
            if known is None:issues.append({'code':'missing_purchase_line','ids':[str(frozen['line_id'])]})
            continue
        received=sum(line.receipt_lines.values_list('quantity_milli',flat=True));base_received=sum(line.receipt_lines.values_list('base_quantity_milli',flat=True))
        row_state={'line_id':line.id,'received_before_milli':received,'received_before_base_milli':base_received,
            'invoice_quantity_milli':line.quantity_milli,'invoice_base_quantity_milli':line.base_quantity_milli,'invoice_total_fcfa':line.total_fcfa,
            'charge_allocations':[],'current_charge_allocations':[]}
        try:row_state['current_charge_allocations']=current_receipt_charge_allocations(line)
        except OriginReferenceConflict:issues.append({'code':'purchase_receipt_cost_context_invalid','ids':[str(line.id)]})
        state.append(row_state)
        delta=frozen['base_quantity_milli'] if known is None else 0;deltas[line.product_id]=deltas.get(line.product_id,0)+delta
        if known is not None or not decoded.has_proof:continue
        basis=frozen['cost_basis'];scaled=frozen['quantity_milli']*line.factor_milli
        expected_allocations=sorted(json_value(basis['charge_allocations']),key=lambda row:row['allocation_id'])
        if sorted(row_state['current_charge_allocations'],key=lambda row:row['allocation_id'])!=expected_allocations:
            notices.append({'code':'purchase_receipt_cost_context_revised','ids':[str(line.id)]})
        if (line.product_id!=frozen['product_id'] or line.product.unit_kind!=frozen['unit_kind']
                or scaled%1000 or scaled//1000!=frozen['base_quantity_milli']
                or line.base_quantity_milli!=basis['invoice_base_quantity_milli'] or line.total_fcfa!=basis['invoice_total_fcfa']
                or base_received!=basis['received_before_base_milli']):issues.append({'code':'purchase_receipt_context_changed','ids':[str(line.id)]})
        if received+frozen['quantity_milli']>line.quantity_milli:issues.append({'code':'purchase_receipt_quantity_exceeded','ids':[str(line.id)]})
        for allocation in basis['charge_allocations']:
            matches=_historical_allocation_matches(shop,document,line,allocation)
            row_state['charge_allocations'].append({'allocation_id':allocation['allocation_id'],'present':matches})
            if not matches:issues.append({'code':'missing_purchase_charge_allocation','ids':[str(allocation['allocation_id'])]})
        adjustments=list(StockMovement.objects.filter(product=line.product,kind='inventory_adjustment',occurred_at__gte=capture.occurred_at,
            operation__created_at__gte=capture.session.generation.created_at).values_list('operation_id',flat=True))
        if adjustments:issues.append({'code':'existing_regularization_review_required','ids':[str(value) for value in adjustments]})
        prepared.append((line,frozen['quantity_milli'],frozen['base_quantity_milli']))
    device=Installation.objects.filter(id=capture.origin_installation_id).values('user_id').first()
    if device and device['user_id']!=capture.origin_user_id:issues.append({'code':'origin_identity_conflict','ids':[]})
    if not verified and not notices:notices.append({'code':'declared_origin_installation','ids':[]})
    stocks=[]
    for product in with_stock(Product.objects.filter(shop=shop,id__in=deltas)).order_by('id'):
        delta=deltas[product.id];after=int(product.stock_validated_milli)+delta
        if after>MAX_FCFA:issues.append({'code':'stock_balance_out_of_range','ids':[str(product.id)]})
        stocks.append({'product_id':product.id,'reservation_id':None,'validated_before_milli':product.stock_validated_milli,
            'reserved_before_milli':product.stock_reserved_milli,'available_before_milli':product.stock_available_milli,
            'validated_after_milli':str(after),'reserved_after_milli':product.stock_reserved_milli,
            'available_after_milli':str(int(product.stock_available_milli)+delta)})
    latest=capture.integration_decisions.order_by('-version').first();version=latest.version if latest else 0
    current=None if document is None else {'document_id':document.id,'lines':state}
    frame=json_value({'capture_id':capture.id,'source_sha256':capture.content_sha256,'version':version,'state':current,'stocks':stocks,
        'issues':issues,'notices':notices,'origin_installation_verified':verified,'known_receipt':known.details.get('response') if known else None})
    preview=json_value({'capture_id':capture.id,'operation_type':'purchase_receipt','version':version,
        'preview_token':hashlib.sha256(canonical_bytes(frame)).hexdigest(),'can_apply':not issues,
        'outcome':'blocked' if issues else 'already_known' if known else 'new','hold_id':None,'review_state':None,'review_version':0,
        'can_hold':False,'can_refuse':False,'snapshot':snapshot,'current_state':current,'stocks':stocks,'cash_delta_by_method_fcfa':{},
        'new_debt_fcfa':0,'new_customer_credit_fcfa':0,'new_supplier_debt_fcfa':0,'new_supplier_advance_fcfa':0,'new_supplier_credit_fcfa':0,
        'supplier_debt_delta_fcfa':0,'supplier_advance_delta_fcfa':0,'supplier_credit_delta_fcfa':0,
        'origin_user_id':capture.origin_user_id,'origin_installation_id':capture.origin_installation_id,
        'origin_recorded_at':capture.origin_recorded_at,'origin_installation_verified':verified,'issues':issues,'notices':notices})
    return preview,decoded,known,prepared,document
