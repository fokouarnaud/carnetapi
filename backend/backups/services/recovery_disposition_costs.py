"""Resolve historical cost versions only through the owned physical receipt."""
from stock.models import ReturnCostSnapshot,ReturnCostRevision,ReturnCostReconstruction,StockCostSnapshot
from stock.services.moving_average_cost import returned_cost_nanos


def disposition_cost_context(shop,receipt,snapshot,before,issues,notices):
    """Keep the selected historical version separate from a newer current revision."""
    product_id=receipt.return_line.sale_line.product_id
    base=ReturnCostSnapshot.objects.filter(receipt=receipt).first()
    current=None;selected=None;total=None
    reconstruction=(ReturnCostReconstruction.objects.filter(original=base,batch__product_id=product_id,
        batch__product__shop=shop,batch__operation__shop=shop).first() if base is not None else None)
    if base is not None:
        latest=ReturnCostRevision.objects.filter(origin=base,batch__product_id=product_id,batch__product__shop=shop,
            batch__operation__shop=shop).select_related('origin').order_by('-version').first()
        current=latest or base
        if (ReturnCostRevision.objects.filter(origin=base,version__gt=current.version if latest else 1).exists()
                or reconstruction is None and ReturnCostReconstruction.objects.filter(original=base).exists()):
            issues.append({'code':'receipt_cost_scope_conflict','ids':[]})
    cost=snapshot['cost'];quantity=snapshot['quantity_milli']
    if cost is None:
        notices.append({'code':'legacy_disposition_without_cost_snapshot','ids':[]})
        legacy=base is None or reconstruction is not None
        if (not legacy or receipt.unit_cost_micros!=snapshot['unit_cost_micros']
                or receipt.cost_is_provisional!=snapshot['cost_is_provisional']):issues.append({'code':'decision_cost_context_changed','ids':[]})
    else:
        if base is not None and base.id==cost['receipt_cost_id']:
            if cost['version']==1 and cost['revision_id'] is None:selected=base
            elif cost['revision_id'] is not None:
                selected=ReturnCostRevision.objects.filter(origin=base,id=cost['revision_id'],version=cost['version'],
                    batch__product_id=product_id,batch__product__shop=shop,batch__operation__shop=shop).select_related('origin').first()
        if selected is None:issues.append({'code':'missing_receipt_cost_version','ids':[str(cost['revision_id'] or cost['receipt_cost_id'])]})
        else:
            from backups.services.recovery_return_receipts import _cost_numbers
            numbers=_cost_numbers(cost)
            if (selected.sale_cost_id!=cost['sale_cost_id'] or selected.cost_state!=cost['cost_state']
                    or selected.unit_cost_micros_numerator!=numbers['unit_cost_micros_numerator']
                    or selected.unit_cost_micros_denominator!=numbers['unit_cost_micros_denominator']
                    or cost['origin_kind']!=('reconstructed_legacy' if reconstruction else 'frozen')):
                issues.append({'code':'decision_cost_source_conflict','ids':[]})
            if selected.sale_cost_id and not StockCostSnapshot.objects.filter(id=selected.sale_cost_id,
                    movement__sale_line=receipt.return_line.sale_line,movement__product__shop=shop).exists():
                issues.append({'code':'receipt_cost_scope_conflict','ids':[]})
            original_before=snapshot['after_quantity_milli']+quantity
            if quantity<=original_before<=receipt.quantity_milli:
                original_total=returned_cost_nanos(selected.total_cost_nanos_fcfa,receipt.quantity_milli,
                    receipt.quantity_milli-original_before,quantity)
                if original_total!=numbers['total_cost_nanos_fcfa']:issues.append({'code':'decision_cost_source_conflict','ids':[]})
            else:issues.append({'code':'decision_original_pool_conflict','ids':[]})
            if quantity<=before<=receipt.quantity_milli:
                total=returned_cost_nanos(selected.total_cost_nanos_fcfa,receipt.quantity_milli,receipt.quantity_milli-before,quantity)
                if total!=numbers['total_cost_nanos_fcfa']:issues.append({'code':'decision_cost_context_changed','ids':[]})
            if current is not None and (current.pk!=selected.pk or type(current) is not type(selected)):
                notices.append({'code':'newer_receipt_cost_kept_distinct','ids':[str(current.pk)]})
    return selected,total,current
