"""Exact shop-local matches warn about possible duplicates without merging."""
from django.db.models import Exists, OuterRef, Q

from customers.models import Customer


def with_duplicate_hint(queryset):
    others = Customer.objects.filter(shop_id=OuterRef("shop_id")).exclude(pk=OuterRef("pk"))
    same_name = others.filter(name=OuterRef("name"))
    same_phone = others.filter(phone=OuterRef("phone"), phone__isnull=False).exclude(phone="")
    return queryset.annotate(has_possible_duplicate=Exists(same_name) | Exists(same_phone))


def possible_duplicates_of(customer):
    match = Q(name=customer.name)
    if customer.phone:
        match |= Q(phone=customer.phone)
    return with_duplicate_hint(Customer.objects.filter(shop_id=customer.shop_id).exclude(pk=customer.pk).filter(match)
        ).order_by("created_at", "id")
