"""Review a cancellation and its real compensating source return as one transaction."""
from dataclasses import dataclass
from types import SimpleNamespace
import hashlib
import uuid
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers
from rest_framework.exceptions import APIException,ValidationError
from backups.journal_protocol import JournalDateTimeField,canonical_bytes
from backups.models import RecoveryJournalOperation
from backups.services.recovery_customers import recorded_origin_evidence
from backups.services.recovery_sale_returns import FrozenSaleReturnPayload,decode_sale_return,build_sale_return_preview,RecoverySaleReturnInvalid
from common.api.fields import StrictSerializer,ExactIntegerField
from common.models import Activity
from common.services.operations import json_value
from identity.models import Installation
from purchasing.api.purchases import EventSerializer
from sales.models import Sale,SaleCancellation,SaleReturn,SaleReturnLine
from stock.services.stock_queries import StockTotal


class RecoveryCancellationInvalid(APIException):
    status_code=400
    default_code='recovery_cancellation_invalid'
    default_detail=_("Recovery sale cancellation snapshot is inconsistent.")


class FrozenSaleCancellation(StrictSerializer):
    id=serializers.UUIDField()
    sale_id=serializers.UUIDField()
    sale_return_id=serializers.UUIDField(allow_null=True)
    occurred_at=JournalDateTimeField()
    created_at=JournalDateTimeField()
    compensating_return=FrozenSaleReturnPayload(allow_null=True)


class CancellationPayload(StrictSerializer):
    schema_version=ExactIntegerField(min_value=1,max_value=1)
    original_request=serializers.JSONField()
    cancellation=FrozenSaleCancellation()


class SourceCancellationInput(EventSerializer):
    occurred_at=JournalDateTimeField()
    def validate_occurred_at(self,value):return value


@dataclass(frozen=True)
class DecodedCancellation:
    request:dict
    snapshot:dict
    canonical_request:dict
    child:object
    child_source:object


def decode_sale_cancellation(capture):
    try:
        incoming=CancellationPayload(data=capture.payload);incoming.is_valid(raise_exception=True)
        snapshot=incoming.validated_data['cancellation'];raw=incoming.validated_data['original_request']
        if not isinstance(raw,dict):raise RecoveryCancellationInvalid()
        raw=dict(raw)
        if (uuid.UUID(str(raw.pop('sale_id',snapshot['sale_id'])))!=snapshot['sale_id']
                or uuid.UUID(str(raw.pop('installation_id',capture.origin_installation_id)))!=capture.origin_installation_id):raise RecoveryCancellationInvalid()
        request=SourceCancellationInput(data=raw);request.is_valid(raise_exception=True);data=request.validated_data
        child_operation=uuid.uuid5(snapshot['id'],'cancellation-return-operation')
        if (data['id']!=snapshot['id'] or data['operation_id']!=capture.operation_id or data['operation_id']==child_operation
                or data['occurred_at']!=capture.occurred_at or snapshot['occurred_at']!=capture.occurred_at
                or (snapshot['sale_return_id'] is None)!=(snapshot['compensating_return'] is None)):raise RecoveryCancellationInvalid()
        child=None;child_source=None
        if snapshot['compensating_return'] is not None:
            child_source=SimpleNamespace(operation_id=child_operation,operation_type='sale_return',shop_id=getattr(capture,'shop_id',None),
                origin_user_id=capture.origin_user_id,origin_installation_id=capture.origin_installation_id,occurred_at=capture.occurred_at,
                payload=capture.payload['cancellation']['compensating_return'])
            child=decode_sale_return(child_source)
            if (snapshot['sale_return_id']!=uuid.uuid5(snapshot['id'],'cancellation-return') or child.snapshot['id']!=snapshot['sale_return_id']
                    or child.snapshot['sale_id']!=snapshot['sale_id'] or child.request['reason']!=data['reason']
                    or child.snapshot['created_at']>snapshot['created_at']):raise RecoveryCancellationInvalid()
            for row in child.snapshot['lines']:
                if row['id']!=uuid.uuid5(snapshot['id'],'cancellation-line:'+str(row['sale_line_id'])):raise RecoveryCancellationInvalid()
        return DecodedCancellation(data,snapshot,json_value({**data,'sale_id':snapshot['sale_id'],
            'installation_id':capture.origin_installation_id}),child,child_source)
    except (ValidationError,ValueError,TypeError,KeyError,AttributeError,OverflowError,RecoverySaleReturnInvalid):raise RecoveryCancellationInvalid() from None


def _known_cancellation(shop,capture,decoded,sale,issues,notices):
    operation=Activity.objects.filter(shop=shop,id=capture.operation_id).first();snapshot=decoded.snapshot;verified=False
    if operation is None:
        if Activity.objects.filter(id=capture.operation_id).exists():issues.append({'code':'operation_conflict','ids':[str(capture.operation_id)]})
        return None,False
    try:
        if not isinstance(operation.details,dict):raise ValueError()
        actor,device,verified,origin=recorded_origin_evidence(capture,operation,notices,client_metadata=False)
        receipt=operation.details['response'];initial=operation.details['recovery']['source_snapshot'] if origin else {
            **{key:receipt[key] for key in snapshot if key!='compensating_return'},'compensating_return':json_value(snapshot['compensating_return'])}
        stored=SaleCancellation.objects.filter(id=snapshot['id'],operation=operation,sale=sale).first() if sale else None
        matches=(stored is not None and actor==capture.origin_user_id and device and operation.action=='sale_cancelled'
            and operation.target_id==snapshot['sale_id'] and operation.details.get('request')==decoded.canonical_request
            and initial==json_value(snapshot) and receipt['actor_id']==str(capture.origin_user_id)
            and receipt['operation_id']==str(capture.operation_id) and receipt['reason']==decoded.request['reason']
            and receipt['created_at']==json_value(stored.created_at)
            and all(receipt[key]==json_value(snapshot[key]) for key in ('id','sale_id','sale_return_id','occurred_at'))
            and stored.sale_return_id==snapshot['sale_return_id'] and stored.occurred_at==snapshot['occurred_at'])
    except (KeyError,ValueError,TypeError,AttributeError):matches=False
    if not matches:issues.append({'code':'operation_conflict','ids':[str(capture.operation_id)]})
    return operation,verified


def build_sale_cancellation_preview(shop,capture):
    decoded=decode_sale_cancellation(capture);snapshot=decoded.snapshot;issues=[];notices=[]
    sale=Sale.objects.filter(shop=shop,id=snapshot['sale_id']).filter(Q(customer__isnull=True)|Q(customer__shop=shop)).first()
    known,verified=_known_cancellation(shop,capture,decoded,sale,issues,notices)
    existing=SaleCancellation.objects.filter(sale=sale).first() if sale else None
    child_capture=None;child_preview=None;child_known=None
    if decoded.child is not None:
        child_capture=RecoveryJournalOperation.objects.filter(shop=shop,session=capture.session,operation_id=decoded.child_source.operation_id).first()
        source=decoded.child_source
        if child_capture is not None:
            from backups.services.recovery_integration import _capture
            child_capture=_capture(shop,child_capture.id)
            if (child_capture.operation_type!='sale_return' or child_capture.origin_user_id!=capture.origin_user_id
                    or child_capture.origin_installation_id!=capture.origin_installation_id or child_capture.occurred_at!=capture.occurred_at
                    or canonical_bytes(child_capture.payload)!=canonical_bytes(source.payload)):
                issues.append({'code':'cancellation_child_copy_conflict','ids':[str(child_capture.id)]})
            else:source=child_capture
            if str(capture.operation_id) in child_capture.dependencies:issues.append({'code':'cancellation_child_dependency_cycle','ids':[]})
        if source is decoded.child_source:
            source.id=capture.id;source.session=capture.session;source.content_sha256=hashlib.sha256(canonical_bytes(source.payload)).hexdigest()
            source.origin_recorded_at=decoded.child.snapshot['created_at'];source.dependencies=[];source.integration_decisions=capture.integration_decisions
        child_preview,_child,child_known,_rows,_sale=build_sale_return_preview(shop,source)
        issues.extend(child_preview['issues']);notices.extend(child_preview['notices'])
        if child_known is None and child_capture is None:issues.append({'code':'missing_cancellation_child_capture','ids':[str(source.operation_id)]})
    quantities=[]
    if sale:
        rows=SaleReturnLine.objects.filter(sale_return__sale=sale)
        if snapshot['sale_return_id'] is not None:rows=rows.exclude(sale_return_id=snapshot['sale_return_id'])
        before={row['sale_line_id']:int(row['total']) for row in rows.order_by().values('sale_line_id').annotate(total=StockTotal('quantity_milli'))}
        expected={}
        for line in sale.lines.all():
            remaining=line.base_quantity_milli-before.get(line.id,0)
            quantities.append({'sale_line_id':line.id,'sold_milli':line.base_quantity_milli,'returned_before_milli':before.get(line.id,0),'remaining_milli':remaining})
            if remaining<0:issues.append({'code':'cancellation_return_quantity_conflict','ids':[str(line.id)]})
            if remaining>0:expected[line.id]=remaining
        actual={row['sale_line_id']:row['quantity_milli'] for row in decoded.child.snapshot['lines']} if decoded.child else {}
        if actual!=expected:issues.append({'code':'cancellation_remainder_conflict','ids':[]})
    if known is None:
        if sale is None:issues.append({'code':'missing_sale','ids':[str(snapshot['sale_id'])]})
        else:
            if existing is not None:issues.append({'code':'sale_already_cancelled','ids':[str(existing.id)]})
            if capture.occurred_at<sale.occurred_at:issues.append({'code':'cancellation_precedes_sale','ids':[]})
            if sale.returns.exclude(id=snapshot['sale_return_id']).filter(occurred_at__gt=capture.occurred_at).exists():
                issues.append({'code':'cancellation_timeline_conflict','ids':[]})
        if capture.occurred_at>timezone.now() or snapshot['created_at']>timezone.now():issues.append({'code':'future_effective_date','ids':[]})
        internal={str(decoded.child_source.operation_id)} if decoded.child_source else set()
        requested=set(capture.dependencies)-internal
        ready={str(value) for value in Activity.objects.filter(shop=shop,id__in=requested,details__has_key='response').values_list('id',flat=True)}
        missing=sorted(requested-ready)
        if missing:issues.append({'code':'missing_dependencies','ids':missing})
        if SaleCancellation.objects.filter(id=snapshot['id']).exists():issues.append({'code':'entity_uuid_conflict','ids':[]})
    installation=Installation.objects.filter(id=capture.origin_installation_id).values('user_id').first()
    if installation and installation['user_id']!=capture.origin_user_id:issues.append({'code':'origin_identity_conflict','ids':[]})
    if not verified:notices.append({'code':'declared_origin_installation','ids':[]})
    latest=capture.integration_decisions.order_by('-version').first();version=latest.version if latest else 0
    state=None if sale is None else {'sale_id':sale.id,'cancelled':existing is not None,'lines':quantities,
        'child_capture_id':child_capture.id if child_capture else None,'child_outcome':child_preview['outcome'] if child_preview else None,
        'child_preview_token':child_preview['preview_token'] if child_preview else None,'compensating_return_state':child_preview['current_state'] if child_preview else None}
    frame=json_value({'capture_id':capture.id,'source_sha256':capture.content_sha256,'version':version,'state':state,'issues':issues,'notices':notices,
        'known_receipt':known.details.get('response') if known and isinstance(known.details,dict) else None})
    preview=json_value({'capture_id':capture.id,'operation_type':'sale_cancellation','version':version,
        'preview_token':hashlib.sha256(canonical_bytes(frame)).hexdigest(),'can_apply':not issues,'outcome':'blocked' if issues else 'already_known' if known else 'new',
        'hold_id':None,'review_state':None,'review_version':0,'can_hold':False,'can_refuse':False,'snapshot':snapshot,'current_state':state,
        'stocks':[],'cash_delta_by_method_fcfa':{},'new_debt_fcfa':0,'new_customer_credit_fcfa':child_preview['new_customer_credit_fcfa'] if child_preview else 0,
        'origin_installation_verified':verified,'issues':issues,'notices':notices,'origin_user_id':capture.origin_user_id,
        'origin_installation_id':capture.origin_installation_id,'origin_recorded_at':capture.origin_recorded_at})
    return preview,decoded,known,{'child_capture':child_capture,'child_preview':child_preview,'child_known':child_known},sale


def recover_cancellation_child(*,shop,actor,live,capture,decoded,plan,request):
    if decoded.child is None:return None,[]
    related=[];child_capture=plan['child_capture']
    if child_capture is not None:
        from backups.services.recovery_integration import integrate_recovery_operation
        child_request={'operation_id':str(uuid.uuid5(uuid.UUID(request['operation_id']),'cancellation-child-review')),
            'previous_version':plan['child_preview']['version'],'preview_token':plan['child_preview']['preview_token'],'reason':request['reason']}
        receipt,_=integrate_recovery_operation(shop_id=shop.id,actor=actor,session_id=live.id,capture_id=child_capture.id,data=child_request)
        related=[receipt['id']]
    returned=SaleReturn.objects.get(id=decoded.snapshot['sale_return_id'],sale_id=decoded.snapshot['sale_id'],sale__shop=shop)
    return returned,related
