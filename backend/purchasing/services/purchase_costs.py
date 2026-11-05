"""Read latest allocated purchase costs independently from cash and supplier obligations."""
from django.db.models import IntegerField, Min, OuterRef, Subquery
from purchasing.models import PurchaseChargeAllocation, PurchaseChargeRevision, PurchaseReceiptLine
from stock.models import StockCostSnapshot
from stock.models import CostRevaluation
from catalogue.models import Product


def invoice_line_cost_fcfa(line):
    latest = PurchaseChargeRevision.objects.filter(charge_id=OuterRef("revision__charge_id")).order_by("-version").values("version")[:1]
    allocations = PurchaseChargeAllocation.objects.filter(line=line, revision__version=Subquery(latest))
    return line.total_fcfa + sum(allocations.values_list("amount_fcfa", flat=True).iterator(chunk_size=1000))


def historical_costs_need_review(line_ids):
    """Conservatively flag existing product costs after affected physical receipts."""
    received = PurchaseReceiptLine.objects.filter(line_id__in=line_ids)
    earliest = received.aggregate(at=Min("movement__occurred_at"))["at"]
    if earliest is None:
        return False
    return StockCostSnapshot.objects.filter(movement__product_id__in=received.values("movement__product_id"),
        movement__occurred_at__gte=earliest).exists()


def current_charge_cost_review_pending(revision):
    """Close a current fee review only with a completed replay for every affected product."""
    pending = revision.operation.details.get("historical_costs_pending_review", False)
    if not pending or PurchaseChargeRevision.objects.filter(charge_id=revision.charge_id, version__gt=revision.version).exists():
        return pending
    affected_lines = PurchaseChargeAllocation.objects.filter(revision__charge_id=revision.charge_id).values("line_id")
    received = PurchaseReceiptLine.objects.filter(line_id__in=affected_lines)
    products = Product.objects.filter(id__in=received.values("movement__product_id"))
    latest = CostRevaluation.objects.filter(product_id=OuterRef("id"), operation__details__cost_revaluation_complete=True
        ).order_by("-created_at", "-id")
    proofs = products.annotate(replayed_sources=Subquery(latest.values("source_revision_ids")[:1]),
        incomplete_returns=Subquery(latest.values("operation__details__cost_revaluation_incomplete_returns")[:1], output_field=IntegerField()))
    for sources, incomplete in proofs.values_list("replayed_sources", "incomplete_returns").iterator(chunk_size=1000):
        if not sources or str(revision.id) not in sources or incomplete is None or incomplete > 0:
            return True
    return False
