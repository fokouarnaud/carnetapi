"""Recover actual physical return receipts, preserving original exact cost lineage."""
from dataclasses import dataclass
import hashlib
import uuid
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import APIException,ValidationError
from backups.journal_protocol import JournalDateTimeField,canonical_bytes
from backups.services.recovery_customers import recorded_origin_evidence
from catalogue.models import Product
from common.api.fields import StrictSerializer,ExactIntegerField,StrictBoolean
from common.models import Activity
from common.services.operations import json_value
from identity.models import Installation
from sales.models import SaleReturnLine
from sales.services.pricing import MAX_FCFA
from stock.api.return_receipts import ReturnReceiptInput
from stock.models import ReturnReceipt,ReturnCostSnapshot,ReturnCostReconstruction,StockCostSnapshot,StockMovement
from stock.services.moving_average_cost import returned_cost_nanos
from stock.services.return_costs import integral_unit_cost
from stock.services.stock_queries import with_stock


class RecoveryReturnReceiptInvalid(APIException):
    status_code=400
    default_code='recovery_return_receipt_invalid'
    default_detail=_("Recovery physical return receipt snapshot is inconsistent.")


class CostIntegerString(serializers.RegexField):
    def __init__(self,**kwargs):super().__init__(r'^(0|[1-9][0-9]*)$',max_length=4096,**kwargs)
    def to_internal_value(self,data):
        if type(data) is not str:self.fail('invalid')
        return super().to_internal_value(data)


class FrozenReceiptCost(StrictSerializer):
    receipt_cost_id=serializers.UUIDField()
    revision_id=serializers.UUIDField(allow_null=True)
    version=ExactIntegerField(min_value=1,max_value=1)
    origin_kind=serializers.ChoiceField(choices=['frozen'])
    sale_cost_id=serializers.UUIDField(allow_null=True)
    cost_state=serializers.ChoiceField(choices=['known','provisional','unknown'])
    total_cost_nanos_fcfa=CostIntegerString(allow_null=True)
    unit_cost_micros_numerator=CostIntegerString(allow_null=True)
    unit_cost_micros_denominator=CostIntegerString(allow_null=True)


class FrozenReturnReceipt(StrictSerializer):
    id=serializers.UUIDField()
    sale_id=serializers.UUIDField()
    sale_return_id=serializers.UUIDField()
    return_line_id=serializers.UUIDField()
    product_id=serializers.UUIDField()
    unit_kind=serializers.ChoiceField(choices=Product.UnitKind.choices)
    quantity_milli=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    condition=serializers.ChoiceField(choices=ReturnReceipt._meta.get_field('condition').choices)
    occurred_at=JournalDateTimeField()
    created_at=JournalDateTimeField()
    movement_id=serializers.UUIDField(allow_null=True)
    unit_cost_micros=ExactIntegerField(min_value=0,max_value=9223372036854775807,allow_null=True)
    cost_is_provisional=StrictBoolean()
    cost=FrozenReceiptCost(allow_null=True)


class ReceiptPayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    receipt=FrozenReturnReceipt()


class SourceReceiptInput(ReturnReceiptInput):
    occurred_at=JournalDateTimeField()
    def validate_occurred_at(self,value):return value


@dataclass(frozen=True)
class DecodedReturnReceipt:
    request:dict
    snapshot:dict
    canonical_request:dict


def _cost_numbers(cost):
    return {key:None if cost[key] is None else int(cost[key]) for key in
        ('total_cost_nanos_fcfa','unit_cost_micros_numerator','unit_cost_micros_denominator')}


def decode_return_receipt(capture):
    try:
        incoming=ReceiptPayload(data=capture.payload);incoming.is_valid(raise_exception=True)
        snapshot=incoming.validated_data['receipt'];raw=incoming.validated_data['original_request']
        if not isinstance(raw,dict):raise RecoveryReturnReceiptInvalid()
        raw=dict(raw)
        if (uuid.UUID(str(raw.pop('return_line_id',snapshot['return_line_id'])))!=snapshot['return_line_id']
                or uuid.UUID(str(raw.pop('installation_id',capture.origin_installation_id)))!=capture.origin_installation_id):raise RecoveryReturnReceiptInvalid()
        request=SourceReceiptInput(data=raw);request.is_valid(raise_exception=True);data=request.validated_data
        movement=uuid.uuid5(snapshot['id'],'return-stock') if snapshot['condition']=='sellable' else None
        if (any(data[key]!=snapshot[key] for key in ('id','quantity_milli','condition','occurred_at'))
                or data['operation_id']!=capture.operation_id or capture.occurred_at!=snapshot['occurred_at']
                or snapshot['movement_id']!=movement or snapshot['unit_kind']=='piece' and snapshot['quantity_milli']%1000):raise RecoveryReturnReceiptInvalid()
        cost=snapshot['cost']
        if cost is not None:
            numbers=_cost_numbers(cost);unknown=cost['cost_state']=='unknown'
            if (cost['revision_id'] is not None or (unknown and any(value is not None for value in numbers.values()))
                    or (not unknown and (any(value is None for value in numbers.values()) or numbers['unit_cost_micros_denominator']==0))
                    or integral_unit_cost(numbers)!=snapshot['unit_cost_micros']
                    or snapshot['cost_is_provisional']!=(cost['cost_state']!='known')):raise RecoveryReturnReceiptInvalid()
        return DecodedReturnReceipt(data,snapshot,json_value({**data,'return_line_id':snapshot['return_line_id'],
            'installation_id':capture.origin_installation_id}))
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):raise RecoveryReturnReceiptInvalid() from None


def _known_receipt(shop,capture,decoded,line,issues,notices):
    operation=Activity.objects.filter(shop=shop,id=capture.operation_id).first()
    if operation is None:
        if Activity.objects.filter(id=capture.operation_id).exists():issues.append({'code':'operation_conflict','ids':[str(capture.operation_id)]})
        return None,False
    snapshot=decoded.snapshot;verified=False
    try:
        if not isinstance(operation.details,dict):raise ValueError()
        actor,device,verified,origin=recorded_origin_evidence(capture,operation,notices,client_metadata=False)
        receipt=operation.details['response']
        initial=(operation.details['recovery']['source_snapshot'] if origin else {
            **{key:receipt[key] for key in snapshot if key not in ('sale_id','sale_return_id','product_id','unit_kind')},
            'sale_id':str(line.sale_return.sale_id),'sale_return_id':str(line.sale_return_id),
            'product_id':str(line.sale_line.product_id),'unit_kind':line.unit_kind})
        stored=ReturnReceipt.objects.select_related('movement','cost_snapshot').filter(id=snapshot['id'],return_line=line,operation=operation).first() if line else None
        matches=(stored is not None and actor==capture.origin_user_id and device and operation.action=='return_received'
            and operation.target_id==snapshot['id'] and operation.details.get('request')==decoded.canonical_request
            and initial==json_value(snapshot) and receipt['actor_id']==str(capture.origin_user_id) and receipt['reason']==decoded.request['reason']
            and receipt['created_at']==json_value(stored.created_at)
            and all(receipt[key]==json_value(snapshot[key]) for key in ('id','return_line_id','quantity_milli','condition','occurred_at','movement_id','unit_cost_micros','cost_is_provisional','cost'))
            and all(getattr(stored,key)==snapshot[key] for key in ('quantity_milli','condition','occurred_at','movement_id','unit_cost_micros','cost_is_provisional')))
        if matches:
            baseline=getattr(stored,'cost_snapshot',None);cost=snapshot['cost']
            if cost is None:
                reconstruction=(ReturnCostReconstruction.objects.filter(original=baseline,batch__product_id=snapshot['product_id'],
                    batch__product__shop=shop,batch__operation__shop=shop).first() if baseline is not None else None)
                matches=(baseline is None or reconstruction is not None
                    and reconstruction.previous_legacy_unit_cost_micros==snapshot['unit_cost_micros']
                    and reconstruction.previous_cost_is_provisional==snapshot['cost_is_provisional'])
            else:
                matches=(baseline is not None and baseline.id==cost['receipt_cost_id']
                    and baseline.sale_cost_id==cost['sale_cost_id'] and baseline.cost_state==cost['cost_state']
                    and all(getattr(baseline,key)==value for key,value in _cost_numbers(cost).items()))
            if stored.movement_id:
                movement=stored.movement
                matches=matches and (movement.product_id==snapshot['product_id'] and movement.operation_id==capture.operation_id
                    and movement.kind=='customer_return' and movement.quantity_milli==snapshot['quantity_milli']
                    and movement.unit_cost_micros==snapshot['unit_cost_micros'] and movement.occurred_at==snapshot['occurred_at'])
    except (KeyError,ValueError,TypeError,AttributeError):matches=False
    if not matches:issues.append({'code':'operation_conflict','ids':[str(capture.operation_id)]})
    return operation,verified


def build_return_receipt_preview(shop,capture):
    decoded=decode_return_receipt(capture);snapshot=decoded.snapshot;issues=[];notices=[];cost_values=None
    line=SaleReturnLine.objects.filter(id=snapshot['return_line_id'],sale_return_id=snapshot['sale_return_id'],
        sale_return__sale_id=snapshot['sale_id'],sale_return__sale__shop=shop,sale_line__sale_id=snapshot['sale_id'],
        sale_line__product_id=snapshot['product_id'],sale_line__product__shop=shop).select_related('sale_return__sale','sale_line').first()
    known,verified=_known_receipt(shop,capture,decoded,line,issues,notices)
    received=sum(line.receipts.values_list('quantity_milli',flat=True)) if line else 0
    all_received=sum(ReturnReceipt.objects.filter(return_line__sale_line=line.sale_line).values_list('quantity_milli',flat=True)) if line else 0
    cost=snapshot['cost'];origin=None;expected=None
    if line:
        if cost and cost['sale_cost_id']:
            origin=StockCostSnapshot.objects.filter(id=cost['sale_cost_id'],movement__sale_line=line.sale_line,movement__product__shop=shop).first()
        if known is None:
            if line.unit_kind!=snapshot['unit_kind'] or line.sale_line.unit_kind!=snapshot['unit_kind']:
                issues.append({'code':'return_unit_conflict','ids':[]})
            if received+snapshot['quantity_milli']>line.quantity_milli or all_received+snapshot['quantity_milli']>line.original_quantity_milli:
                issues.append({'code':'return_receipt_quantity_exceeded','ids':[]})
            if capture.occurred_at<line.sale_return.sale.occurred_at:issues.append({'code':'receipt_precedes_sale','ids':[]})
            if cost and cost['sale_cost_id'] and origin is None:issues.append({'code':'missing_sale_cost','ids':[str(cost['sale_cost_id'])]})
            elif all_received+snapshot['quantity_milli']<=line.original_quantity_milli:
                micros=line.sale_line.unit_cost_micros
                total=origin.total_cost_nanos_fcfa if origin else None if micros is None else micros*line.original_quantity_milli
                state=origin.cost_state if origin else 'unknown' if micros is None else 'provisional' if line.sale_return.sale.stock_requires_regularization else 'known'
                expected={'sale_cost':origin,'cost_state':state,
                    'total_cost_nanos_fcfa':returned_cost_nanos(total,line.original_quantity_milli,all_received,snapshot['quantity_milli']),
                    'unit_cost_micros_numerator':origin.unit_cost_micros_numerator if origin else micros,
                    'unit_cost_micros_denominator':origin.unit_cost_micros_denominator if origin else None if micros is None else 1}
                if cost is None:
                    notices.append({'code':'legacy_receipt_without_cost_snapshot','ids':[]})
                    if snapshot['unit_cost_micros']!=micros:issues.append({'code':'return_cost_context_changed','ids':[]})
                elif cost['cost_state']!=state or any(_cost_numbers(cost)[key]!=expected[key] for key in _cost_numbers(cost)):
                    issues.append({'code':'return_cost_context_changed','ids':[]})
                else:cost_values=expected
    if known is None:
        if capture.occurred_at>timezone.now() or snapshot['created_at']>timezone.now():issues.append({'code':'future_effective_date','ids':[]})
        if line is None:issues.append({'code':'missing_return_line','ids':[str(snapshot['return_line_id'])]})
        ready={str(value) for value in Activity.objects.filter(shop=shop,id__in=capture.dependencies,details__has_key='response').values_list('id',flat=True)}
        missing=sorted(set(capture.dependencies)-ready)
        if missing:issues.append({'code':'missing_dependencies','ids':missing})
        if (ReturnReceipt.objects.filter(id=snapshot['id']).exists() or snapshot['movement_id'] and StockMovement.objects.filter(id=snapshot['movement_id']).exists()
                or cost and ReturnCostSnapshot.objects.filter(id=cost['receipt_cost_id']).exists()):issues.append({'code':'entity_uuid_conflict','ids':[]})
        if snapshot['condition']=='sellable':
            adjustments=list(StockMovement.objects.filter(product__shop=shop,product_id=snapshot['product_id'],kind='inventory_adjustment',
                occurred_at__gte=capture.occurred_at,operation__created_at__gte=capture.session.generation.created_at).values_list('operation_id',flat=True))
            if adjustments:issues.append({'code':'existing_regularization_review_required','ids':[str(value) for value in adjustments]})
    installation=Installation.objects.filter(id=capture.origin_installation_id).values('user_id').first()
    if installation and installation['user_id']!=capture.origin_user_id:issues.append({'code':'origin_identity_conflict','ids':[]})
    if not verified and not notices:notices.append({'code':'declared_origin_installation','ids':[]})
    product=with_stock(Product.objects.filter(shop=shop,id=snapshot['product_id'])).first() if line else None
    stocks=[];delta=snapshot['quantity_milli'] if known is None and snapshot['condition']=='sellable' else 0
    if product:
        after=int(product.stock_validated_milli)+delta
        if after>MAX_FCFA:issues.append({'code':'stock_balance_out_of_range','ids':[]})
        stocks=[{'product_id':product.id,'reservation_id':None,'validated_before_milli':product.stock_validated_milli,
            'reserved_before_milli':product.stock_reserved_milli,'available_before_milli':product.stock_available_milli,
            'validated_after_milli':str(after),'reserved_after_milli':product.stock_reserved_milli,
            'available_after_milli':str(int(product.stock_available_milli)+delta)}]
    state=None if line is None else {'return_line_id':line.id,'product_id':line.sale_line.product_id,'received_before_milli':received,
        'sale_received_before_milli':all_received,'return_quantity_milli':line.quantity_milli,'original_quantity_milli':line.original_quantity_milli,
        'sale_cost_id':origin.id if origin else None,'expected_cost':None if expected is None else {
            'cost_state':expected['cost_state'],**{key:None if expected[key] is None else str(expected[key]) for key in
                ('total_cost_nanos_fcfa','unit_cost_micros_numerator','unit_cost_micros_denominator')}}}
    latest=capture.integration_decisions.order_by('-version').first();version=latest.version if latest else 0
    frame=json_value({'capture_id':capture.id,'source_sha256':capture.content_sha256,'version':version,'state':state,'stocks':stocks,
        'issues':issues,'notices':notices,'origin_installation_verified':verified,'known_receipt':known.details.get('response') if known else None})
    preview=json_value({'capture_id':capture.id,'operation_type':'return_receipt','version':version,
        'preview_token':hashlib.sha256(canonical_bytes(frame)).hexdigest(),'can_apply':not issues,'outcome':'blocked' if issues else 'already_known' if known else 'new',
        'hold_id':None,'review_state':None,'review_version':0,'can_hold':False,'can_refuse':False,'snapshot':snapshot,'current_state':state,'stocks':stocks,
        'cash_delta_by_method_fcfa':{},'new_debt_fcfa':0,'new_customer_credit_fcfa':0,'origin_installation_verified':verified,'issues':issues,'notices':notices,
        'origin_user_id':capture.origin_user_id,'origin_installation_id':capture.origin_installation_id,'origin_recorded_at':capture.origin_recorded_at})
    return preview,decoded,known,cost_values,line
