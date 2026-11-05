"""Recover supplier identity and three separate opening positions under current rights."""
import hashlib
import uuid
from django.db.models import Q
from django.utils import timezone
from backups.journal_protocol import canonical_bytes
from backups.services.recovery_customers import recorded_origin_evidence
from backups.services.recovery_supplier_snapshots import decode_supplier_operation
from common.models import Activity
from common.services.operations import json_value
from identity.models import Installation
from purchasing.models import Supplier,SupplierDebt,SupplierCredit,SupplierLedgerEntry


def _opening_kind(capture,snapshot):
    return 'opening_debt' if capture.operation_type=='supplier_opening_debt' else snapshot['kind']


def _opening_ledger_matches(shop,supplier,operation,capture,snapshot):
    kind=_opening_kind(capture,snapshot);amount=snapshot['amount_fcfa']
    row=SupplierLedgerEntry.objects.filter(shop=shop,supplier=supplier,operation=operation).first()
    deltas=(amount,0,0) if kind=='opening_debt' else (0,amount,0) if kind=='opening_advance' else (0,0,amount)
    return (row is not None and SupplierLedgerEntry.objects.filter(operation=operation).count()==1
        and row.id==uuid.uuid5(snapshot['id'],'supplier-ledger:'+kind) and row.source_id==snapshot['id']
        and row.kind==kind and row.sequence==0 and row.state=='validated' and row.external_reference==''
        and row.occurred_at==capture.occurred_at and row.recorded_at==operation.created_at
        and (row.debt_delta_fcfa,row.advance_delta_fcfa,row.credit_delta_fcfa)==deltas
        and row.delta_fcfa==deltas[0]-deltas[1]-deltas[2])


def _known_supplier_operation(shop,capture,decoded,supplier,issues,notices):
    operation=Activity.objects.filter(shop=shop,id=capture.operation_id).first();snapshot=decoded.snapshot;verified=False
    if operation is None:
        if Activity.objects.filter(id=capture.operation_id).exists():issues.append({'code':'operation_conflict','ids':[str(capture.operation_id)]})
        return None,False
    is_creation=capture.operation_type=='supplier_creation';is_debt=capture.operation_type=='supplier_opening_debt'
    action='supplier_created' if is_creation else 'supplier_debt_opened' if is_debt else 'supplier_balance_opened'
    try:
        if not isinstance(operation.details,dict):raise ValueError()
        actor,device,verified,origin=recorded_origin_evidence(capture,operation,notices,client_metadata=False)
        receipt=operation.details['response']
        initial=operation.details['recovery']['source_snapshot'] if origin else {
            **{key:receipt[key] for key in snapshot if key!='supplier_id'},**({} if is_creation else {'supplier_id':str(snapshot['supplier_id'])})}
        matches=(actor==capture.origin_user_id and device and operation.action==action and operation.target_id==snapshot['id']
            and operation.details.get('request')==decoded.canonical_request and initial==json_value(snapshot) and supplier is not None)
        if is_creation:
            matches=(matches and receipt==json_value(snapshot)
                and not operation.supplier_ledger.exists() and not operation.cash_movements.exists())
        else:
            if is_debt:
                stored=SupplierDebt.objects.filter(shop=shop,supplier=supplier,id=snapshot['id'],operation=operation,document__isnull=True).first()
                matches=matches and stored is not None and stored.amount_fcfa==snapshot['amount_fcfa']
            else:
                stored=SupplierCredit.objects.filter(opening_shop=shop,opening_supplier=supplier,id=snapshot['id'],operation=operation,supplier_return__isnull=True).first()
                matches=(matches and stored is not None and stored.kind==snapshot['kind'] and stored.amount_fcfa==snapshot['amount_fcfa']
                    and stored.reference_fcfa is None and stored.applied_to_origin_fcfa==0 and receipt['actor_id']==str(capture.origin_user_id)
                    and receipt['reason']==decoded.request['reason'])
            matches=(matches and stored.occurred_at==capture.occurred_at and receipt['created_at']==json_value(stored.created_at)
                and all(receipt[key]==json_value(snapshot[key]) for key in snapshot if key not in ('supplier_id','created_at'))
                and operation.reason==decoded.request['reason'] and not operation.cash_movements.exists()
                and _opening_ledger_matches(shop,supplier,operation,capture,snapshot))
    except (KeyError,ValueError,TypeError,AttributeError):matches=False
    if not matches:issues.append({'code':'operation_conflict','ids':[str(capture.operation_id)]})
    return operation,verified


def build_supplier_operation_preview(shop,capture):
    decoded=decode_supplier_operation(capture);snapshot=decoded.snapshot;issues=[];notices=[]
    is_creation=capture.operation_type=='supplier_creation';is_debt=capture.operation_type=='supplier_opening_debt'
    supplier=Supplier.objects.filter(shop=shop,id=snapshot['id'] if is_creation else snapshot['supplier_id']).first()
    known,verified=_known_supplier_operation(shop,capture,decoded,supplier,issues,notices)
    duplicate=False
    if is_creation:
        same=Q(name__iexact=snapshot['name'])
        if snapshot['phone'] is not None:same|=Q(phone=snapshot['phone'])
        duplicate=Supplier.objects.filter(shop=shop).exclude(id=snapshot['id']).filter(same).exists()
        notices.append({'code':'declared_supplier_creation_time','ids':[]})
    if not verified:notices.append({'code':'declared_origin_installation','ids':[]})
    latest=capture.integration_decisions.order_by('-version').first();version=latest.version if latest else 0
    if known is None:
        if capture.occurred_at>timezone.now() or not is_creation and snapshot['created_at']>timezone.now():issues.append({'code':'future_effective_date','ids':[]})
        if is_creation:
            if Supplier.objects.filter(id=snapshot['id']).exists():issues.append({'code':'entity_uuid_conflict','ids':[]})
        else:
            if supplier is None:issues.append({'code':'missing_supplier','ids':[str(snapshot['supplier_id'])]})
            model=SupplierDebt if is_debt else SupplierCredit;kind=_opening_kind(capture,snapshot)
            if model.objects.filter(id=snapshot['id']).exists() or SupplierLedgerEntry.objects.filter(id=uuid.uuid5(snapshot['id'],'supplier-ledger:'+kind)).exists():
                issues.append({'code':'entity_uuid_conflict','ids':[]})
        ready={str(value) for value in Activity.objects.filter(shop=shop,id__in=capture.dependencies,details__has_key='response').values_list('id',flat=True)}
        missing=sorted(set(capture.dependencies)-ready)
        if missing:issues.append({'code':'missing_dependencies','ids':missing})
    source_device=Installation.objects.filter(id=capture.origin_installation_id).values('user_id').first()
    if source_device is not None and source_device['user_id']!=capture.origin_user_id:issues.append({'code':'origin_identity_conflict','ids':[]})
    state=None if supplier is None else {'supplier_id':supplier.id,'version':supplier.version}
    frame=json_value({'capture_id':capture.id,'source_sha256':capture.content_sha256,'version':version,'state':state,
        'known_receipt':known.details.get('response') if known and isinstance(known.details,dict) else None,
        'issues':issues,'notices':notices,'origin_installation_verified':verified,'has_possible_duplicate':duplicate})
    amount=snapshot['amount_fcfa'] if not is_creation and known is None else 0
    preview=json_value({'capture_id':capture.id,'operation_type':capture.operation_type,'version':version,
        'preview_token':hashlib.sha256(canonical_bytes(frame)).hexdigest(),'can_apply':not issues,
        'outcome':'blocked' if issues else 'already_known' if known else 'new','hold_id':None,'review_state':None,'review_version':0,
        'can_hold':False,'can_refuse':False,'snapshot':snapshot,'current_state':state,'stocks':[],'cash_delta_by_method_fcfa':{},
        'new_debt_fcfa':0,'new_customer_credit_fcfa':0,'new_supplier_debt_fcfa':amount if is_debt else 0,
        'new_supplier_advance_fcfa':amount if not is_creation and not is_debt and snapshot['kind']=='opening_advance' else 0,
        'new_supplier_credit_fcfa':amount if not is_creation and not is_debt and snapshot['kind']=='opening_credit' else 0,
        'origin_user_id':capture.origin_user_id,'origin_installation_id':capture.origin_installation_id,'origin_recorded_at':capture.origin_recorded_at,
        'origin_installation_verified':verified,'has_possible_duplicate':duplicate,'issues':issues,'notices':notices})
    return preview,decoded,known,{},supplier
