"""Bounded SQL projections of optional unit and pack codes for local catalogues."""
from django.db.models import (BigIntegerField, BooleanField, CharField, Exists, ExpressionWrapper, F, Func,
                              OuterRef, Q, Subquery, Value)
from django.db.models.functions import Coalesce
from catalogue.models import Barcode


# Everything below expresses quantities in the product unit: once one exists,
# changing the unit would change the meaning of recorded facts (14 Nov 2026).
UNIT_LOCKING_RELATIONS = ("packs", "stock_reservations", "losses", "stock_counts", "shortage_reviews",
                          "valuation_adjustments", "cost_revaluations", "transfer_departures",
                          "transfer_arrivals", "sale_lines", "purchaseline_set", "pack_price_conflicts")


def unit_lock_expression():
    """True once the product has any movement but its opening, or any fact in its unit."""
    from catalogue.models import Product
    from stock.models import StockMovement
    locked = Q(Exists(StockMovement.objects.filter(product_id=OuterRef("pk")).exclude(kind="opening")))
    for relation in Product._meta.related_objects:
        if relation.get_accessor_name() in UNIT_LOCKING_RELATIONS:
            locked |= Q(Exists(relation.related_model._default_manager.filter(**{relation.field.name: OuterRef("pk")})))
    return ExpressionWrapper(locked, output_field=BooleanField())


def with_codes(queryset, pack=False):
    if pack:
        codes = Barcode.objects.filter(pack_id=OuterRef("pk"), product_id=OuterRef("product_id"),
                                       shop_id=OuterRef("product__shop_id"))
    else:
        codes = Barcode.objects.filter(product_id=OuterRef("pk"), pack__isnull=True, shop_id=OuterRef("shop_id"))
    if not pack:
        queryset = queryset.annotate(unit_locked=unit_lock_expression())
    return queryset.annotate(
        barcode=Subquery(codes.values("code")[:1], output_field=CharField()),
        barcode_version=Coalesce(Subquery(codes.values("version")[:1]), Value(0), output_field=BigIntegerField()),
    )


from rest_framework import serializers


class NameSearchInput(serializers.Serializer):
    search = serializers.CharField(max_length=150, required=False, default="", allow_blank=True)


def register_catalogue_functions(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        connection.connection.create_function("catalogue_name_contains", 2,
            lambda name, term: term.casefold() in name.casefold(), deterministic=True)


def filter_by_name(queryset, query_params):
    serializer = NameSearchInput(data={"search": query_params.get("search", "")})
    serializer.is_valid(raise_exception=True)
    term = serializer.validated_data["search"]
    if not term:
        return queryset
    return queryset.alias(name_matches=Func(F("name"), Value(term),
        function="catalogue_name_contains", output_field=BooleanField())).filter(name_matches=True)
