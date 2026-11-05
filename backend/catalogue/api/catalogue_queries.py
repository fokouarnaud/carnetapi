"""Bounded SQL projections of optional unit and pack codes for local catalogues."""
from django.db.models import BigIntegerField, BooleanField, CharField, F, Func, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from catalogue.models import Barcode


def with_codes(queryset, pack=False):
    if pack:
        codes = Barcode.objects.filter(pack_id=OuterRef("pk"), product_id=OuterRef("product_id"),
                                       shop_id=OuterRef("product__shop_id"))
    else:
        codes = Barcode.objects.filter(product_id=OuterRef("pk"), pack__isnull=True, shop_id=OuterRef("shop_id"))
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
