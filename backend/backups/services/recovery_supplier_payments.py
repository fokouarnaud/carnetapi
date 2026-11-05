"""Review real supplier outflows separately from exact noncash debt allocations."""
import hashlib
import uuid
from django.db.models import Q
from django.utils import timezone
from backups.journal_protocol import canonical_bytes
from backups.services.recovery_customers import recorded_origin_evidence
from backups.services.recovery_supplier_payment_snapshots import decode_supplier_payment,decode_supplier_allocation,immutable_payment_receipt,immutable_allocation_receipt
from cash.models import CashMovement
from common.models import Activity
from common.services.operations import json_value
from identity.models import Installation
from purchasing.models import Supplier,SupplierPayment,SupplierPaymentPart,SupplierPaymentAllocation,SupplierDebt,SupplierLedgerEntry


def _operation(shop,capture,issues):
    operation=Activity.objects.filter(shop=shop,id=capture.operation_id).first()
    if operation is None and Activity.objects.filter(id=capture.operation_id).exists():issues.append({'code':'operation_conflict','ids':[str(capture.operation_id)]})
    return operation


def _ledger_matches(shop,supplier,operation,source_id,kind,occurred,amount):
    row=SupplierLedgerEntry.objects.filter(shop=shop,supplier=supplier,operation=operation).first()
    debt,advance=(0,amount) if kind=='payment' else (-amount,-amount)
    return (row is not None and SupplierLedgerEntry.objects.filter(operation=operation).count()==1
        and row.id==uuid.uuid5(source_id,'supplier-ledger:'+kind) and row.source_id==source_id and row.kind==kind
        and row.state=='validated' and row.sequence==0 and row.external_reference==''
        and row.occurred_at==occurred and row.recorded_at==operation.created_at
        and (row.delta_fcfa,row.debt_delta_fcfa,row.advance_delta_fcfa,row.credit_delta_fcfa)==(debt-advance,debt,advance,0))


def _known_payment(shop,capture,decoded,supplier,issues,notices):
    operation=_operation(shop,capture,issues);snapshot=decoded.snapshot;verified=False
    if operation is None:return None,False
    try:
        if not isinstance(operation.details,dict):raise ValueError()
        actor,device,verified,origin=recorded_origin_evidence(capture,operation,notices,client_metadata=False)
        receipt=operation.details['response'];initial=operation.details['recovery']['source_snapshot'] if origin else immutable_payment_receipt(receipt)
        payment=SupplierPayment.objects.filter(shop=shop,supplier=supplier,id=snapshot['id'],operation=operation).first() if supplier else None
        matches=(payment is not None and actor==capture.origin_user_id and device and initial==json_value(snapshot)
            and operation.action=='supplier_payment_created' and operation.target_id==snapshot['id']
            and operation.reason==decoded.request['reason'] and operation.details.get('request')==decoded.canonical_request
            and payment.amount_fcfa==snapshot['amount_fcfa'] and payment.occurred_at==capture.occurred_at
            and receipt['created_at']==json_value(payment.created_at)
            and all(receipt[key]==json_value(snapshot[key]) for key in snapshot if key!='created_at'))
        if matches:
            parts=list(payment.parts.order_by('position').values('id','method','other_name','external_reference','amount_fcfa'))
            matches=json_value(parts)==json_value(snapshot['parts'])
            rows=list(CashMovement.objects.filter(shop=shop,operation=operation,supplier_payment_part__payment=payment))
            by_part={row.supplier_payment_part_id:row for row in rows}
            matches=matches and len(rows)==len(parts) and operation.cash_movements.count()==len(parts)
            for part in parts:
                row=by_part.get(part['id'])
                matches=(matches and row is not None and row.method==part['method'] and row.amount_fcfa==-part['amount_fcfa']
                    and row.occurred_at==capture.occurred_at and row.actor_id==operation.actor_id and row.installation_id==payment.installation_id
                    and row.manual_kind=='' and row.reason==decoded.request['reason']
                    and (not origin or row.id==uuid.uuid5(part['id'],'supplier-payment-cash')))
            matches=matches and _ledger_matches(shop,supplier,operation,payment.id,'payment',capture.occurred_at,snapshot['amount_fcfa'])
    except (KeyError,ValueError,TypeError,AttributeError):matches=False
    if not matches:issues.append({'code':'operation_conflict','ids':[str(capture.operation_id)]})
    return operation,verified


def _known_allocation(shop,capture,decoded,payment,supplier,issues,notices):
    operation=_operation(shop,capture,issues);snapshot=decoded.snapshot;verified=False
    if operation is None:return None,False
    try:
        if not isinstance(operation.details,dict):raise ValueError()
        actor,device,verified,origin=recorded_origin_evidence(capture,operation,notices,client_metadata=False)
        receipt=operation.details['response'];initial=operation.details['recovery']['source_snapshot'] if origin else immutable_allocation_receipt(receipt,supplier.id)
        matches=(payment is not None and actor==capture.origin_user_id and device and initial==json_value(snapshot)
            and operation.action=='supplier_payment_allocated' and operation.target_id==snapshot['payment_id']
            and operation.reason==decoded.request['reason'] and operation.details.get('request')==decoded.canonical_request
            and receipt['operation_id']==str(capture.operation_id) and receipt['payment_id']==str(payment.id)
            and receipt['allocated_fcfa']==snapshot['allocated_fcfa'] and receipt['recorded_at']==json_value(operation.created_at))
        if matches:
            expected_after=operation.details['recovery']['replay_after_unallocated_fcfa'] if origin else snapshot['after_unallocated_fcfa']
            matches=receipt['after_unallocated_fcfa']==expected_after
            rows=SupplierPaymentAllocation.objects.filter(operation=operation,payment=payment,debt__shop=shop,debt__supplier=supplier).order_by('position')
            actual=list(rows.values('id','debt_id','amount_fcfa'))
            matches=(matches and json_value(actual)==json_value(snapshot['allocations']) and operation.supplier_allocations.count()==len(actual)
                and [{key:row[key] for key in ('id','debt_id','amount_fcfa')} for row in receipt['allocations']]==json_value(actual)
                and all(row['actor_id']==str(capture.origin_user_id) and row['operation_id']==str(operation.id)
                    and row['reason']==decoded.request['reason'] and row['recorded_at']==json_value(operation.created_at) for row in receipt['allocations'])
                and not operation.cash_movements.exists()
                and _ledger_matches(shop,supplier,operation,operation.id,'payment_allocation',capture.occurred_at,snapshot['allocated_fcfa']))
    except (KeyError,ValueError,TypeError,AttributeError):matches=False
    if not matches:issues.append({'code':'operation_conflict','ids':[str(capture.operation_id)]})
    return operation,verified


def _source_checks(shop,capture,issues,known):
    if known is None:
        ready={str(value) for value in Activity.objects.filter(shop=shop,id__in=capture.dependencies,details__has_key='response').values_list('id',flat=True)}
        missing=sorted(set(capture.dependencies)-ready)
        if missing:issues.append({'code':'missing_dependencies','ids':missing})
        if capture.occurred_at>timezone.now():issues.append({'code':'future_effective_date','ids':[]})
    device=Installation.objects.filter(id=capture.origin_installation_id).values('user_id').first()
    if device is not None and device['user_id']!=capture.origin_user_id:issues.append({'code':'origin_identity_conflict','ids':[]})


def build_supplier_payment_preview(shop,capture):
    is_payment=capture.operation_type=='supplier_payment'
    decoded=decode_supplier_payment(capture) if is_payment else decode_supplier_allocation(capture)
    snapshot=decoded.snapshot;issues=[];notices=[];impacts=[];prepared=[]
    supplier=Supplier.objects.filter(shop=shop,id=snapshot['supplier_id']).first()
    payment=None if is_payment else SupplierPayment.objects.filter(shop=shop,supplier=supplier,id=snapshot['payment_id'],operation__shop=shop).first()
    known,verified=(_known_payment(shop,capture,decoded,supplier,issues,notices) if is_payment else
        _known_allocation(shop,capture,decoded,payment,supplier,issues,notices))
    if not verified:notices.append({'code':'declared_origin_installation','ids':[]})
    _source_checks(shop,capture,issues,known)
    duplicate=False;cash={};delta=0
    state=None if supplier is None else {'supplier_id':supplier.id,'version':supplier.version}
    if is_payment:
        if known is None:
            if supplier is None:issues.append({'code':'missing_supplier','ids':[str(snapshot['supplier_id'])]})
            ids=[row['id'] for row in snapshot['parts']]
            if (SupplierPayment.objects.filter(id=snapshot['id']).exists() or SupplierPaymentPart.objects.filter(id__in=ids).exists()
                    or CashMovement.objects.filter(id__in=[uuid.uuid5(value,'supplier-payment-cash') for value in ids]).exists()
                    or SupplierLedgerEntry.objects.filter(id=uuid.uuid5(snapshot['id'],'supplier-ledger:payment')).exists()):
                issues.append({'code':'entity_uuid_conflict','ids':[]})
            if snapshot['created_at']>timezone.now():issues.append({'code':'future_creation_date','ids':[]})
            for row in snapshot['parts']:
                key='other:'+row['other_name'] if row['method']=='other' else row['method']
                cash[key]=cash.get(key,0)-row['amount_fcfa']
            delta=snapshot['amount_fcfa']
        for row in snapshot['parts']:
            if row['external_reference'] and SupplierPaymentPart.objects.filter(payment__shop=shop,payment__supplier=supplier,
                    method=row['method'],other_name=row['other_name'],external_reference=row['external_reference']).exclude(payment_id=snapshot['id']).exists():duplicate=True
    else:
        if payment is None:
            issues.append({'code':'missing_payment','ids':[str(snapshot['payment_id'])]});state=None
        else:
            state={**state,'payment_id':payment.id,'unallocated_fcfa':payment.unallocated_fcfa}
        debts=SupplierDebt.objects.filter(shop=shop,supplier=supplier,id__in=[row['debt_id'] for row in snapshot['allocations']])
        debts=debts.filter(Q(document__isnull=True)|Q(document__shop=shop,document__supplier=supplier)).in_bulk()
        for row in snapshot['allocations']:
            debt=debts.get(row['debt_id']);amount=row['amount_fcfa'];enough=debt is not None and debt.remaining_fcfa>=amount
            impacts.append({'debt_id':row['debt_id'],'requested_fcfa':amount,'remaining_before_fcfa':debt.remaining_fcfa if debt else None,
                'remaining_after_fcfa':debt.remaining_fcfa-(amount if known is None else 0) if debt and (enough or known is not None) else None,
                'can_apply':bool(enough or known is not None)})
            if known is None:
                if debt is None:issues.append({'code':'missing_debt','ids':[str(row['debt_id'])]})
                elif not enough:issues.append({'code':'supplier_debt_insufficient','ids':[str(debt.id)]})
                else:prepared.append((debt,amount))
        if payment is not None:state['debts']=impacts
        if known is None:
            if payment and payment.unallocated_fcfa<snapshot['allocated_fcfa']:issues.append({'code':'supplier_advance_insufficient','ids':[str(payment.id)]})
            if payment and capture.occurred_at<payment.occurred_at:issues.append({'code':'allocation_precedes_payment','ids':[]})
            if (SupplierPaymentAllocation.objects.filter(id__in=[row['id'] for row in snapshot['allocations']]).exists()
                    or SupplierLedgerEntry.objects.filter(id=uuid.uuid5(capture.operation_id,'supplier-ledger:payment_allocation')).exists()):
                issues.append({'code':'entity_uuid_conflict','ids':[]})
            delta=-snapshot['allocated_fcfa']
    latest=capture.integration_decisions.order_by('-version').first();version=latest.version if latest else 0
    frame=json_value({'capture_id':capture.id,'source_sha256':capture.content_sha256,'version':version,'state':state,'issues':issues,
        'notices':notices,'known_receipt':known.details.get('response') if known and isinstance(known.details,dict) else None,
        'origin_installation_verified':verified,'has_possible_duplicate':duplicate})
    preview=json_value({'capture_id':capture.id,'operation_type':capture.operation_type,'version':version,
        'preview_token':hashlib.sha256(canonical_bytes(frame)).hexdigest(),'can_apply':not issues,'outcome':'blocked' if issues else 'already_known' if known else 'new',
        'hold_id':None,'review_state':None,'review_version':0,'can_hold':False,'can_refuse':False,'snapshot':snapshot,'current_state':state,
        'stocks':[],'cash_delta_by_method_fcfa':cash,'new_debt_fcfa':0,'new_customer_credit_fcfa':0,'new_supplier_debt_fcfa':0,
        'new_supplier_advance_fcfa':max(0,delta),'new_supplier_credit_fcfa':0,'supplier_debt_delta_fcfa':0 if is_payment else delta,
        'supplier_advance_delta_fcfa':delta,'supplier_credit_delta_fcfa':0,'debt_allocation_impacts':impacts,
        'origin_user_id':capture.origin_user_id,'origin_installation_id':capture.origin_installation_id,'origin_recorded_at':capture.origin_recorded_at,
        'origin_installation_verified':verified,'has_possible_duplicate':duplicate,'issues':issues,'notices':notices})
    return preview,decoded,known,prepared,supplier if is_payment else payment
