"""Expose historical origin references without creating authentication identities."""
from django.db.models import F
from django.db.models.functions import Coalesce
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import APIException


class OriginReferenceConflict(APIException):
    status_code=409
    default_code="origin_reference_conflict"
    default_detail=_("Stored operation origin conflicts with its shop. Review the source linkage.")


def with_public_origin(queryset,*,actor_path="actor_id",installation_path="installation_id",operation_path="operation"):
    """Project source identities after scoping, refusing contradictory lineage."""
    if operation_path and queryset.exclude(**{operation_path+'__shop_id':F('shop_id')}).exists():
        raise OriginReferenceConflict()
    origin=(operation_path+"__" if operation_path else "")+"recovered_origin__capture__"
    link=(operation_path+"__" if operation_path else "")+"recovered_origin"
    if queryset.filter(**{link+"__isnull":False}).exclude(**{origin+"shop_id":F("shop_id")}).exists():
        raise OriginReferenceConflict()
    fields={"public_actor_id":Coalesce(F(origin+"origin_user_id"),F(actor_path))}
    if installation_path is not None:
        fields["public_installation_id"]=Coalesce(F(origin+"origin_installation_id"),F(installation_path))
    return queryset.annotate(**fields)


def public_identity(obj,context,field):
    origin=context.get("origin_identity",{})
    return str(origin.get(field,getattr(obj,"public_"+field,getattr(obj,field))))
