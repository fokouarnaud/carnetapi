"""Preview historical orders and invoices without receiving goods or moving money."""
import hashlib
import uuid

from django.utils import timezone

from backups.journal_protocol import canonical_bytes
from backups.services.recovery_customers import recorded_origin_evidence
from backups.services.recovery_purchase_snapshots import decode_purchase,immutable_purchase_receipt
from catalogue.models import Product,ProductPack
from common.models import Activity
from common.services.operations import json_value
from identity.models import Installation
from purchasing.models import PurchaseDocument,PurchaseLine,Supplier,SupplierDebt,SupplierLedgerEntry


LINE_FACTS=('id','product_id','pack_id','product_name','unit_label','quantity_milli','factor_milli',
            'base_quantity_milli','unit_price_fcfa','total_fcfa')


def _invoice_ledger_matches(shop,supplier,operation,snapshot):
    row=SupplierLedgerEntry.objects.filter(shop=shop,supplier=supplier,operation=operation).first()
    return (row is not None and SupplierLedgerEntry.objects.filter(operation=operation).count()==1
        and row.id==uuid.uuid5(snapshot['id'],'supplier-ledger:invoice') and row.source_id==snapshot['id']
        and row.kind=='invoice' and row.state=='validated' and row.sequence==0
        and row.external_reference==snapshot['external_reference'] and row.occurred_at==snapshot['occurred_at']
        and row.recorded_at==operation.created_at
        and (row.delta_fcfa,row.debt_delta_fcfa,row.advance_delta_fcfa,row.credit_delta_fcfa)==(snapshot['total_fcfa'],snapshot['total_fcfa'],0,0))


def _known_purchase(shop,capture,decoded,supplier,issues,notices):
    snapshot=decoded.snapshot;operation=Activity.objects.filter(shop=shop,id=capture.operation_id).first();verified=False
    if operation is None:
        if Activity.objects.filter(id=capture.operation_id).exists():issues.append({'code':'operation_conflict','ids':[str(capture.operation_id)]})
        return None,False
    try:
        if not isinstance(operation.details,dict):raise ValueError()
        actor,device,verified,origin=recorded_origin_evidence(capture,operation,notices,client_metadata=False)
        receipt=operation.details['response']
        initial=operation.details['recovery']['source_snapshot'] if origin else immutable_purchase_receipt(receipt,snapshot['supplier_id'])
        document=PurchaseDocument.objects.filter(shop=shop,supplier=supplier,operation=operation,id=snapshot['id']).first() if supplier else None
        matches=(document is not None and actor==capture.origin_user_id and device and initial==json_value(snapshot)
            and operation.action=='purchase_'+snapshot['kind']+'_created' and operation.target_id==snapshot['id']
            and operation.reason==decoded.request['reason'] and operation.details.get('request')==decoded.canonical_request
            and document.kind==snapshot['kind'] and document.order_id==snapshot['order_id']
            and document.external_reference==snapshot['external_reference'] and document.total_fcfa==snapshot['total_fcfa']
            and document.occurred_at==capture.occurred_at and receipt['created_at']==json_value(document.created_at)
            and not operation.cash_movements.exists() and not operation.stock_movements.exists())
        if matches:
            actual=list(document.lines.order_by('position').values(*LINE_FACTS))
            expected=[{key:row[key] for key in LINE_FACTS} for row in snapshot['lines']]
            matches=json_value(actual)==json_value(expected)
            relations=document.lines.values('product__shop_id','product__unit_kind','pack_id','pack__product_id','product_id','base_quantity_milli')
            for row in relations:
                matches=(matches and row['product__shop_id']==shop.id
                    and (row['pack_id'] is None or row['pack__product_id']==row['product_id'])
                    and (row['product__unit_kind']!='piece' or row['base_quantity_milli']%1000==0))
            matches=(matches and all(receipt[key]==json_value(snapshot[key]) for key in ('id','kind','order_id','occurred_at','external_reference','total_fcfa','lines')))
        if matches and snapshot['kind']=='order':
            matches=(receipt['debt'] is None and not SupplierDebt.objects.filter(operation=operation).exists() and not operation.supplier_ledger.exists())
        elif matches:
            frozen=snapshot['debt'];debt=SupplierDebt.objects.filter(shop=shop,supplier=supplier,operation=operation,document=document,id=frozen['id']).first()
            matches=(debt is not None and debt.amount_fcfa==frozen['amount_fcfa'] and debt.occurred_at==capture.occurred_at
                and debt.installation_id==document.installation_id and receipt['debt']['created_at']==json_value(debt.created_at)
                and all(receipt['debt'][key]==json_value(frozen[key]) for key in frozen if key!='created_at')
                and _invoice_ledger_matches(shop,supplier,operation,snapshot))
        if matches and document.order_id is not None:
            matches=PurchaseDocument.objects.filter(id=document.order_id,kind='order',shop=shop,supplier=supplier,operation__shop=shop).exists()
    except (KeyError,ValueError,TypeError,AttributeError):matches=False
    if not matches:issues.append({'code':'operation_conflict','ids':[str(capture.operation_id)]})
    return operation,verified


def build_purchase_preview(shop,capture):
    decoded=decode_purchase(capture);snapshot=decoded.snapshot;issues=[];notices=[];prepared=[]
    supplier=Supplier.objects.filter(shop=shop,id=snapshot['supplier_id']).first()
    known,verified=_known_purchase(shop,capture,decoded,supplier,issues,notices)
    if not verified:notices.append({'code':'declared_origin_installation','ids':[]})
    order=PurchaseDocument.objects.filter(id=snapshot['order_id'],kind='order',shop=shop,supplier=supplier,operation__shop=shop).first() if snapshot['order_id'] else None
    products=Product.objects.filter(shop=shop,id__in=[row['product_id'] for row in snapshot['lines']]).in_bulk()
    packs=ProductPack.objects.filter(product__shop=shop,id__in=[row['pack_id'] for row in snapshot['lines'] if row['pack_id']]).in_bulk()
    if known is None:
        if supplier is None:issues.append({'code':'missing_supplier','ids':[str(snapshot['supplier_id'])]})
        if snapshot['order_id'] is not None and order is None:issues.append({'code':'missing_order','ids':[str(snapshot['order_id'])]})
        if capture.occurred_at>timezone.now() or snapshot['created_at']>timezone.now() or snapshot['debt'] and snapshot['debt']['created_at']>timezone.now():
            issues.append({'code':'future_effective_date','ids':[]})
        ready={str(value) for value in Activity.objects.filter(shop=shop,id__in=capture.dependencies,details__has_key='response').values_list('id',flat=True)}
        missing=sorted(set(capture.dependencies)-ready)
        if missing:issues.append({'code':'missing_dependencies','ids':missing})
        if (PurchaseDocument.objects.filter(id=snapshot['id']).exists()
                or PurchaseLine.objects.filter(id__in=[row['id'] for row in snapshot['lines']]).exists()
                or snapshot['debt'] is not None and SupplierDebt.objects.filter(id=snapshot['debt']['id']).exists()
                or snapshot['kind']=='invoice' and SupplierLedgerEntry.objects.filter(id=uuid.uuid5(snapshot['id'],'supplier-ledger:invoice')).exists()):
            issues.append({'code':'entity_uuid_conflict','ids':[]})
        for position,row in enumerate(snapshot['lines']):
            product=products.get(row['product_id']);pack=packs.get(row['pack_id'])
            if product is None:issues.append({'code':'missing_product','ids':[str(row['product_id'])]})
            if row['pack_id'] is not None and (pack is None or pack.product_id!=row['product_id']):issues.append({'code':'missing_pack','ids':[str(row['pack_id'])]})
            if product is not None and product.unit_kind=='piece' and row['base_quantity_milli']%1000:issues.append({'code':'product_unit_conflict','ids':[str(product.id)]})
            if product is not None and (row['pack_id'] is None or pack is not None and pack.product_id==product.id):
                prepared.append({key:row[key] for key in LINE_FACTS if key not in ('product_id','pack_id')}|{'product':product,'pack':pack,'position':position})
    device=Installation.objects.filter(id=capture.origin_installation_id).values('user_id').first()
    if device is not None and device['user_id']!=capture.origin_user_id:issues.append({'code':'origin_identity_conflict','ids':[]})
    latest=capture.integration_decisions.order_by('-version').first();version=latest.version if latest else 0
    state={'supplier_id':supplier.id,'supplier_version':supplier.version} if supplier else None
    if state is not None:
        state.update(order_id=order.id if order else None,
            products=sorted([{'id':row.id,'unit_kind':row.unit_kind} for row in products.values()],key=lambda row:str(row['id'])),
            packs=sorted([{'id':row.id,'product_id':row.product_id} for row in packs.values()],key=lambda row:str(row['id'])))
    frame=json_value({'capture_id':capture.id,'source_sha256':capture.content_sha256,'version':version,'state':state,
        'known_receipt':known.details.get('response') if known and isinstance(known.details,dict) else None,
        'issues':issues,'notices':notices,'origin_installation_verified':verified})
    amount=snapshot['total_fcfa'] if known is None and snapshot['kind']=='invoice' else 0
    preview=json_value({'capture_id':capture.id,'operation_type':capture.operation_type,'version':version,
        'preview_token':hashlib.sha256(canonical_bytes(frame)).hexdigest(),'can_apply':not issues,
        'outcome':'blocked' if issues else 'already_known' if known else 'new','hold_id':None,'review_state':None,'review_version':0,
        'can_hold':False,'can_refuse':False,'snapshot':snapshot,'current_state':state,'stocks':[],
        'cash_delta_by_method_fcfa':{},'new_debt_fcfa':0,'new_customer_credit_fcfa':0,
        'new_supplier_debt_fcfa':amount,'new_supplier_advance_fcfa':0,'new_supplier_credit_fcfa':0,
        'supplier_debt_delta_fcfa':amount,'supplier_advance_delta_fcfa':0,'supplier_credit_delta_fcfa':0,
        'origin_user_id':capture.origin_user_id,'origin_installation_id':capture.origin_installation_id,
        'origin_recorded_at':capture.origin_recorded_at,'origin_installation_verified':verified,'issues':issues,'notices':notices})
    return preview,decoded,known,prepared,supplier
