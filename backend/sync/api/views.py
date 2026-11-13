"""Downstream change feed: rights are checked on every page (G6)."""
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from common.services.operations import current_shop
from identity.services.access import effective_permissions
from identity.services.authentication import require_live_session
from sync.models import ChangeFeedEntry
from sync.services import visible_to_seller

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


class ChangeQuerySerializer(serializers.Serializer):
    after = serializers.IntegerField(min_value=0)
    limit = serializers.IntegerField(min_value=1, required=False, default=DEFAULT_LIMIT)


class ChangeEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = ChangeFeedEntry
        fields = ["seq", "object_type", "object_id", "kind", "created_at"]


def product_payloads(shop, entries, can_read_cost, context):
    """Current product representations, serialized exactly like the catalogue for this reader (G7)."""
    from catalogue.api.catalogue_queries import with_codes
    from catalogue.api.catalogue_serializers import CostProductSerializer, PublicProductSerializer
    from catalogue.api.price_conflicts import with_price_conflict_metadata
    from catalogue.models import Product
    from stock.services.stock_queries import with_stock
    ids = {entry.object_id for entry in entries if entry.object_type.startswith("product_")}
    if not ids:
        return {}
    products = with_price_conflict_metadata(with_codes(with_stock(Product.objects.filter(shop=shop, id__in=ids))))
    serializer = CostProductSerializer if can_read_cost else PublicProductSerializer
    return {product.id: serializer(product, context=context).data for product in products.select_related("photo_record")}


def customer_payloads(shop, entries):
    """Current customer records of the shop; customers are shared reference data for sellers (G7)."""
    from customers.api.client_serializers import ClientSerializer
    from customers.models import Customer
    ids = {entry.object_id for entry in entries if entry.object_type.startswith("client_")}
    if not ids:
        return {}
    return {customer.id: ClientSerializer(customer).data for customer in Customer.objects.filter(shop=shop, id__in=ids)}


def sale_payloads(shop, entries, can_read_debt):
    """Sales serialized like the sales list; credit sales only for readers allowed to see debts (G7)."""
    from common.services.origins import with_public_origin
    from sales.api.sale_serializers import SaleSerializer
    from sales.models import Sale
    ids = {entry.object_id for entry in entries if entry.object_type.startswith("sale_")}
    if not ids:
        return {}
    sales = Sale.objects.filter(shop=shop, id__in=ids)
    if not can_read_debt:
        sales = sales.filter(mode="cash", customer_credit__isnull=True)
    sales = with_public_origin(sales).select_related("debt", "debt__shop", "customer_credit", "cancellation").prefetch_related("lines", "payments")
    return {sale.id: SaleSerializer(sale, context={}).data for sale in sales}


def debt_payloads(shop, entries):
    """Debts serialized like the debt list; entries reach a seller only with can_read_debt (G7)."""
    from customers.api.debts import CustomerDebtSerializer
    from customers.models import CustomerDebt
    ids = {entry.object_id for entry in entries if entry.object_type.startswith("customer_debt_")}
    if not ids:
        return {}
    return {debt.id: CustomerDebtSerializer(debt).data for debt in CustomerDebt.objects.filter(shop=shop, id__in=ids)}


class ChangeFeedView(APIView):
    @extend_schema(parameters=[ChangeQuerySerializer], responses=ChangeEntrySerializer(many=True))
    def get(self, request, shop_id):
        session = request.device_session
        require_live_session(request.user, session.id, session.installation)
        shop = current_shop(shop_id, request.user, session.installation)
        query = ChangeQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        after, limit = query.validated_data["after"], min(query.validated_data["limit"], MAX_LIMIT)
        rows = list(ChangeFeedEntry.objects.filter(shop=shop, seq__gt=after).order_by("seq")[:limit + 1])
        scanned = rows[:limit]
        role, permissions = effective_permissions(shop, request.user)
        visible = scanned if role == "manager" else [
            row for row in scanned if visible_to_seller(row, request.user.id, permissions["can_read_debt"])]
        results = ChangeEntrySerializer(visible, many=True).data
        payloads = {"product_": product_payloads(shop, visible, permissions["can_read_cost"], {"request": request}),
                    "client_": customer_payloads(shop, visible),
                    "sale_": sale_payloads(shop, visible, permissions["can_read_debt"]),
                    "customer_debt_": debt_payloads(shop, visible)}
        for result, entry in zip(results, visible):
            prefix = next((key for key in payloads if entry.object_type.startswith(key)), None)
            result["data"] = payloads[prefix].get(entry.object_id) if prefix else None
        # The cursor passes hidden entries too, so a page is never rescanned.
        return Response({
            "results": results,
            "next_after": scanned[-1].seq if scanned else after,
            "has_more": len(rows) > limit,
        })
