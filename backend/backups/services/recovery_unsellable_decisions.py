"""Recover physical dispositions with exact quantities and their historical cost basis."""
from dataclasses import dataclass
import hashlib
import uuid
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import APIException,ValidationError
from backups.journal_protocol import JournalDateTimeField,canonical_bytes
from backups.services.recovery_customers import recorded_origin_evidence
from backups.services.recovery_return_receipts import CostIntegerString,_cost_numbers
from backups.services.recovery_disposition_costs import disposition_cost_context
from catalogue.models import Product
from common.api.fields import StrictSerializer,ExactIntegerField,StrictBoolean
from common.models import Activity
from common.services.operations import json_value
from identity.models import Installation
from purchasing.models import Supplier,SupplierLedgerEntry
from sales.services.pricing import MAX_FCFA
from stock.api.unsellable_decisions import NonSellableDecisionInput
from stock.models import ReturnReceipt,NonSellableDecision,StockMovement
from stock.services.return_costs import integral_unit_cost
from stock.services.stock_queries import with_stock
from stock.services.unsellable_balance import remaining_unsellable


class RecoveryDispositionInvalid(APIException):
    status_code=400
    default_code='recovery_disposition_invalid'
    default_detail=_("Recovery non-sellable disposition snapshot is inconsistent.")


class FrozenDispositionCost(StrictSerializer):
    receipt_cost_id=serializers.UUIDField()
    revision_id=serializers.UUIDField(allow_null=True)
    version=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    origin_kind=serializers.ChoiceField(choices=['frozen','reconstructed_legacy'])
    sale_cost_id=serializers.UUIDField(allow_null=True)
    cost_state=serializers.ChoiceField(choices=['known','provisional','unknown'])
    total_cost_nanos_fcfa=CostIntegerString(allow_null=True)
    unit_cost_micros_numerator=CostIntegerString(allow_null=True)
    unit_cost_micros_denominator=CostIntegerString(allow_null=True)


class FrozenUnsellableDecision(StrictSerializer):
    id=serializers.UUIDField()
    receipt_id=serializers.UUIDField()
    sale_id=serializers.UUIDField()
    sale_return_id=serializers.UUIDField()
    return_line_id=serializers.UUIDField()
    product_id=serializers.UUIDField()
    unit_kind=serializers.ChoiceField(choices=Product.UnitKind.choices)
    kind=serializers.ChoiceField(choices=NonSellableDecision.Kind.choices)
    quantity_milli=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    after_quantity_milli=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    is_sellable_confirmed=StrictBoolean()
    supplier_id=serializers.UUIDField(allow_null=True)
    occurred_at=JournalDateTimeField()
    created_at=JournalDateTimeField()
    movement_id=serializers.UUIDField(allow_null=True)
    unit_cost_micros=ExactIntegerField(min_value=0,max_value=9223372036854775807,allow_null=True)
    cost_is_provisional=StrictBoolean()
    cost=FrozenDispositionCost(allow_null=True)


class DispositionPayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    decision=FrozenUnsellableDecision()


class SourceDispositionInput(NonSellableDecisionInput):
    occurred_at=JournalDateTimeField()
    def validate_occurred_at(self,value):return value


@dataclass(frozen=True)
class DecodedDisposition:
    request:dict
    snapshot:dict
    canonical_request:dict


def decode_unsellable_decision(capture):
    try:
        incoming=DispositionPayload(data=capture.payload);incoming.is_valid(raise_exception=True)
        snapshot=incoming.validated_data['decision'];raw=incoming.validated_data['original_request']
        if not isinstance(raw,dict):raise RecoveryDispositionInvalid()
        raw=dict(raw)
        if (uuid.UUID(str(raw.pop('receipt_id',snapshot['receipt_id'])))!=snapshot['receipt_id']
                or uuid.UUID(str(raw.pop('installation_id',capture.origin_installation_id)))!=capture.origin_installation_id):raise RecoveryDispositionInvalid()
        request=SourceDispositionInput(data=raw);request.is_valid(raise_exception=True);data=request.validated_data
        movement=uuid.uuid5(snapshot['id'],'unsellable-restock') if snapshot['kind']=='restock' else None
        if (any(data[key]!=snapshot[key] for key in ('id','kind','quantity_milli','is_sellable_confirmed','supplier_id','occurred_at'))
                or data['operation_id']!=capture.operation_id or snapshot['occurred_at']!=capture.occurred_at
                or snapshot['movement_id']!=movement or snapshot['after_quantity_milli']+snapshot['quantity_milli']>MAX_FCFA
                or snapshot['unit_kind']=='piece' and (snapshot['quantity_milli']%1000 or snapshot['after_quantity_milli']%1000)):
            raise RecoveryDispositionInvalid()
        cost=snapshot['cost']
        if cost is not None:
            numbers=_cost_numbers(cost);unknown=cost['cost_state']=='unknown'
            if ((cost['version']==1)!=(cost['revision_id'] is None)
                    or unknown and any(value is not None for value in numbers.values())
                    or not unknown and (any(value is None for value in numbers.values()) or numbers['unit_cost_micros_denominator']==0)
                    or integral_unit_cost(numbers)!=snapshot['unit_cost_micros']
                    or snapshot['cost_is_provisional']!=(cost['cost_state']!='known')):raise RecoveryDispositionInvalid()
        return DecodedDisposition(data,snapshot,json_value({**data,'receipt_id':snapshot['receipt_id'],'installation_id':capture.origin_installation_id}))
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError):raise RecoveryDispositionInvalid() from None


def _known_decision(shop,capture,decoded,received,issues,notices):
    operation=Activity.objects.filter(shop=shop,id=capture.operation_id).first();snapshot=decoded.snapshot;verified=False;stored=None
    if operation is None:
        if Activity.objects.filter(id=capture.operation_id).exists():issues.append({'code':'operation_conflict','ids':[str(capture.operation_id)]})
        return None,False,None
    try:
        if not isinstance(operation.details,dict):raise ValueError()
        actor,device,verified,origin=recorded_origin_evidence(capture,operation,notices,client_metadata=False)
        receipt=operation.details['response']
        initial=(operation.details['recovery']['source_snapshot'] if origin else {
            **{key:receipt[key] for key in snapshot if key not in ('sale_id','sale_return_id','return_line_id','product_id')},
            'sale_id':str(received.return_line.sale_return.sale_id),'sale_return_id':str(received.return_line.sale_return_id),
            'return_line_id':str(received.return_line_id),'product_id':str(received.return_line.sale_line.product_id)})
        stored=NonSellableDecision.objects.select_related('movement').filter(id=snapshot['id'],receipt=received,operation=operation).first() if received else None
        matches=(stored is not None and actor==capture.origin_user_id and device and operation.action=='unsellable_decided'
            and operation.target_id==snapshot['id'] and operation.details.get('request')==decoded.canonical_request
            and initial==json_value(snapshot) and receipt['actor_id']==str(capture.origin_user_id)
            and receipt['operation_id']==str(capture.operation_id) and receipt['reason']==decoded.request['reason']
            and receipt['created_at']==json_value(stored.created_at) and receipt['after_quantity_milli']==stored.after_quantity_milli
            and all(receipt[key]==json_value(snapshot[key]) for key in ('id','receipt_id','kind','quantity_milli','unit_kind','is_sellable_confirmed',
                'supplier_id','occurred_at','movement_id','unit_cost_micros','cost_is_provisional','cost'))
            and all(getattr(stored,key)==snapshot[key] for key in ('kind','quantity_milli','unit_kind','is_sellable_confirmed','supplier_id','occurred_at','movement_id')))
        if matches and stored.movement_id:
            movement=stored.movement
            matches=(movement.product_id==snapshot['product_id'] and movement.operation_id==capture.operation_id
                and movement.kind=='unsellable_restock' and movement.quantity_milli==snapshot['quantity_milli']
                and movement.unit_cost_micros==snapshot['unit_cost_micros'] and movement.occurred_at==snapshot['occurred_at'])
        if matches and snapshot['supplier_id']:
            row=SupplierLedgerEntry.objects.filter(id=uuid.uuid5(snapshot['id'],'supplier-ledger:unsellable_return'),shop=shop,
                supplier_id=snapshot['supplier_id'],operation=operation,source_id=snapshot['id'],kind='unsellable_return',occurred_at=snapshot['occurred_at']).first()
            matches=row is not None and (row.delta_fcfa,row.debt_delta_fcfa,row.advance_delta_fcfa,row.credit_delta_fcfa)==(0,0,0,0)
    except (KeyError,ValueError,TypeError,AttributeError):matches=False
    if not matches:issues.append({'code':'operation_conflict','ids':[str(capture.operation_id)]})
    return operation,verified,stored


def build_unsellable_decision_preview(shop,capture):
    decoded=decode_unsellable_decision(capture);snapshot=decoded.snapshot;issues=[];notices=[]
    received=ReturnReceipt.objects.filter(id=snapshot['receipt_id'],condition='unsellable',return_line_id=snapshot['return_line_id'],
        return_line__sale_return_id=snapshot['sale_return_id'],return_line__sale_return__sale_id=snapshot['sale_id'],
        return_line__sale_return__sale__shop=shop,return_line__sale_line__sale_id=snapshot['sale_id'],
        return_line__sale_line__product_id=snapshot['product_id'],return_line__sale_line__product__shop=shop
        ).select_related('return_line__sale_return__sale','return_line__sale_line').first()
    known,verified,stored=_known_decision(shop,capture,decoded,received,issues,notices)
    before=remaining_unsellable(received) if received else 0;cost_basis=None;cost_total=None;current_cost=None
    if received:
        context_before=stored.after_quantity_milli+snapshot['quantity_milli'] if known and stored else before
        cost_basis,cost_total,current_cost=disposition_cost_context(shop,received,snapshot,context_before,issues,notices)
    supplier=Supplier.objects.filter(shop=shop,id=snapshot['supplier_id']).first() if snapshot['supplier_id'] else None
    if known is None:
        if received is None:issues.append({'code':'missing_unsellable_receipt','ids':[str(snapshot['receipt_id'])]})
        else:
            if received.return_line.unit_kind!=snapshot['unit_kind']:issues.append({'code':'disposition_unit_conflict','ids':[]})
            if snapshot['quantity_milli']>before:issues.append({'code':'unsellable_quantity_exceeded','ids':[]})
            if snapshot['quantity_milli']+snapshot['after_quantity_milli']>received.quantity_milli:issues.append({'code':'decision_original_pool_conflict','ids':[]})
            if capture.occurred_at<received.occurred_at:issues.append({'code':'disposition_precedes_receipt','ids':[]})
        if snapshot['supplier_id'] and supplier is None:issues.append({'code':'missing_supplier','ids':[str(snapshot['supplier_id'])]})
        if capture.occurred_at>timezone.now() or snapshot['created_at']>timezone.now():issues.append({'code':'future_effective_date','ids':[]})
        ready={str(value) for value in Activity.objects.filter(shop=shop,id__in=capture.dependencies,details__has_key='response').values_list('id',flat=True)}
        missing=sorted(set(capture.dependencies)-ready)
        if missing:issues.append({'code':'missing_dependencies','ids':missing})
        if (NonSellableDecision.objects.filter(id=snapshot['id']).exists() or snapshot['movement_id'] and StockMovement.objects.filter(id=snapshot['movement_id']).exists()
                or SupplierLedgerEntry.objects.filter(id=uuid.uuid5(snapshot['id'],'supplier-ledger:unsellable_return')).exists()):
            issues.append({'code':'entity_uuid_conflict','ids':[]})
        if snapshot['kind']=='restock':
            adjustments=list(StockMovement.objects.filter(product__shop=shop,product_id=snapshot['product_id'],kind='inventory_adjustment',
                occurred_at__gte=capture.occurred_at,operation__created_at__gte=capture.session.generation.created_at).values_list('operation_id',flat=True))
            if adjustments:issues.append({'code':'existing_regularization_review_required','ids':[str(value) for value in adjustments]})
    installation=Installation.objects.filter(id=capture.origin_installation_id).values('user_id').first()
    if installation and installation['user_id']!=capture.origin_user_id:issues.append({'code':'origin_identity_conflict','ids':[]})
    if not verified and not any(row['code']=='legacy_origin_installation_unknown' for row in notices):notices.append({'code':'declared_origin_installation','ids':[]})
    stocks=[];product=with_stock(Product.objects.filter(shop=shop,id=snapshot['product_id'])).first() if received else None
    delta=snapshot['quantity_milli'] if known is None and snapshot['kind']=='restock' else 0
    if product:
        after=int(product.stock_validated_milli)+delta
        if after>MAX_FCFA:issues.append({'code':'stock_balance_out_of_range','ids':[]})
        stocks=[{'product_id':product.id,'reservation_id':None,'validated_before_milli':product.stock_validated_milli,
            'reserved_before_milli':product.stock_reserved_milli,'available_before_milli':product.stock_available_milli,
            'validated_after_milli':str(after),'reserved_after_milli':product.stock_reserved_milli,
            'available_after_milli':str(int(product.stock_available_milli)+delta)}]
    state=None if received is None else {'receipt_id':received.id,'quantity_milli':received.quantity_milli,
        'remaining_before_milli':before,'remaining_after_milli':before if known else None if snapshot['quantity_milli']>before else before-snapshot['quantity_milli'],
        'supplier_id':supplier.id if supplier else None,'selected_cost_id':cost_basis.pk if cost_basis else None,
        'current_cost_id':current_cost.pk if current_cost else None,'current_cost_version':getattr(current_cost,'version',1) if current_cost else None,
        'projected_cost_nanos_fcfa':None if cost_total is None else str(cost_total)}
    latest=capture.integration_decisions.order_by('-version').first();version=latest.version if latest else 0
    frame=json_value({'capture_id':capture.id,'source_sha256':capture.content_sha256,'version':version,'state':state,'stocks':stocks,
        'issues':issues,'notices':notices,'origin_installation_verified':verified,'known_receipt':known.details.get('response') if known and isinstance(known.details,dict) else None})
    preview=json_value({'capture_id':capture.id,'operation_type':'non_sellable_decision','version':version,
        'preview_token':hashlib.sha256(canonical_bytes(frame)).hexdigest(),'can_apply':not issues,'outcome':'blocked' if issues else 'already_known' if known else 'new',
        'hold_id':None,'review_state':None,'review_version':0,'can_hold':False,'can_refuse':False,'snapshot':snapshot,'current_state':state,'stocks':stocks,
        'cash_delta_by_method_fcfa':{},'new_debt_fcfa':0,'new_customer_credit_fcfa':0,'issues':issues,'notices':notices,'origin_installation_verified':verified,
        'origin_user_id':capture.origin_user_id,'origin_installation_id':capture.origin_installation_id,'origin_recorded_at':capture.origin_recorded_at})
    return preview,decoded,known,{'before':before,'supplier':supplier,'cost_basis':cost_basis,'cost_total':cost_total},received
