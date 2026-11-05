"""Current managers review competing product prices without stock or cash effects."""
from django.db import transaction
from django.db.models import BigIntegerField,OuterRef,Subquery,UUIDField
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers,viewsets
from rest_framework.response import Response

from catalogue.models import Product
from common.api.fields import ExactIntegerField,StrictSerializer
from common.services.operations import current_shop,require_manager
from common.services.pagination import BoundedPagination
from identity.services.authentication import require_live_session
from sales.services.pricing import MAX_FCFA


class PublicPriceConflictSerializer(serializers.Serializer):
    id=serializers.UUIDField()
    state=serializers.ChoiceField(choices=['open'])
    common_price_fcfa=serializers.IntegerField(min_value=0,max_value=MAX_FCFA)


def with_price_conflict_metadata(queryset):
    """Project only public conflict facts inside the authorized product SQL query."""
    from catalogue.models import ProductPriceConflict
    active=ProductPriceConflict.objects.filter(product_id=OuterRef('pk'),shop_id=OuterRef('shop_id'),state='open').order_by('created_at','id')
    return queryset.annotate(public_price_conflict_id=Subquery(active.values('id')[:1],output_field=UUIDField()),
        public_price_conflict_common_price_fcfa=Subquery(active.values('common_price_fcfa')[:1],output_field=BigIntegerField()))


def public_price_conflict(product):
    """Use list annotations, with one bounded lookup for unannotated single receipts."""
    if hasattr(product,'public_price_conflict_id'):
        identifier=product.public_price_conflict_id;price=product.public_price_conflict_common_price_fcfa
    else:
        from catalogue.models import ProductPriceConflict
        row=ProductPriceConflict.objects.filter(product_id=product.id,shop_id=product.shop_id,state='open').order_by('created_at','id').values('id','common_price_fcfa').first()
        identifier=row['id'] if row else None;price=row['common_price_fcfa'] if row else None
    return None if identifier is None else {'id':str(identifier),'state':'open','common_price_fcfa':price}


class RecordPriceConflictInput(StrictSerializer):
    operation_id=serializers.UUIDField()
    source_change_id=serializers.UUIDField()
    base_version=ExactIntegerField(min_value=1,max_value=MAX_FCFA-1)
    proposed_price_fcfa=ExactIntegerField(min_value=0,max_value=MAX_FCFA)
    reason=serializers.CharField(max_length=2000,allow_blank=False)


class ResolvePriceConflictInput(StrictSerializer):
    operation_id=serializers.UUIDField()
    preview_token=serializers.CharField(max_length=512,trim_whitespace=False)
    proposal_id=serializers.UUIDField(allow_null=True)
    reason=serializers.CharField(max_length=2000,allow_blank=False)


class PriceProposalSerializer(serializers.Serializer):
    id=serializers.UUIDField()
    operation_id=serializers.UUIDField()
    actor_id=serializers.UUIDField()
    installation_id=serializers.UUIDField(allow_null=True)
    proposed_price_fcfa=serializers.IntegerField(min_value=0,max_value=MAX_FCFA)
    reason=serializers.CharField(max_length=2000)
    created_at=serializers.DateTimeField()


class PriceResolutionSerializer(serializers.Serializer):
    operation_id=serializers.UUIDField()
    proposal_id=serializers.UUIDField(allow_null=True)
    price_fcfa=serializers.IntegerField(min_value=0,max_value=MAX_FCFA)
    actor_id=serializers.UUIDField()
    reason=serializers.CharField(max_length=2000)
    created_at=serializers.DateTimeField()


class ProductPriceConflictSerializer(serializers.Serializer):
    id=serializers.UUIDField()
    product_id=serializers.UUIDField()
    source_change_id=serializers.UUIDField()
    base_version=serializers.IntegerField(min_value=1,max_value=MAX_FCFA-1)
    common_price_fcfa=serializers.IntegerField(min_value=0,max_value=MAX_FCFA)
    state=serializers.ChoiceField(choices=['open','resolved'])
    version=serializers.IntegerField(min_value=1)
    product_version=serializers.IntegerField(min_value=1)
    created_at=serializers.DateTimeField()
    proposals=PriceProposalSerializer(many=True)
    resolution=PriceResolutionSerializer(allow_null=True)


class PriceConflictPreviewSerializer(ProductPriceConflictSerializer):
    preview_token=serializers.CharField(max_length=512,allow_null=True)
    can_resolve=serializers.BooleanField()


class ProductPriceConflictViewSet(viewsets.GenericViewSet):
    serializer_class=ProductPriceConflictSerializer
    pagination_class=BoundedPagination

    def context(self):
        request=self.request
        require_live_session(request.user,request.device_session.id,request.device_session.installation)
        shop=current_shop(self.kwargs['shop_id'],request.user,request.device_session.installation)
        require_manager(shop,request.user)
        return shop,get_object_or_404(Product,shop=shop,id=self.kwargs['product_id'])

    def get_queryset(self):
        from catalogue.models import ProductPriceConflict
        if getattr(self,'swagger_fake_view',False):return ProductPriceConflict.objects.none()
        shop,product=self.context()
        return ProductPriceConflict.objects.filter(shop=shop,product=product).select_related('product','resolution').prefetch_related('proposals').order_by('created_at','id')

    @extend_schema(responses=ProductPriceConflictSerializer(many=True))
    @transaction.atomic
    def list(self,request,*args,**kwargs):
        from catalogue.services.price_conflicts import price_conflict_state
        rows=self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response([price_conflict_state(row) for row in rows])

    @extend_schema(responses=ProductPriceConflictSerializer)
    @transaction.atomic
    def retrieve(self,request,*args,**kwargs):
        from catalogue.services.price_conflicts import price_conflict_state
        return Response(price_conflict_state(self.get_object()))

    @extend_schema(request=RecordPriceConflictInput,responses={200:ProductPriceConflictSerializer,201:ProductPriceConflictSerializer})
    def create(self,request,*args,**kwargs):
        from catalogue.services.price_conflicts import record_price_conflict
        self.context()
        incoming=RecordPriceConflictInput(data=request.data);incoming.is_valid(raise_exception=True)
        result,created=record_price_conflict(self.kwargs['shop_id'],self.kwargs['product_id'],request.user,
            request.device_session.installation,incoming.validated_data,request.device_session.id)
        return Response(result,status=201 if created else 200)

    @extend_schema(responses=PriceConflictPreviewSerializer)
    def preview(self,request,*args,**kwargs):
        from catalogue.services.price_conflicts import preview_price_conflict
        self.context()
        return Response(preview_price_conflict(self.kwargs['shop_id'],self.kwargs['product_id'],self.kwargs['pk'],
            request.user,request.device_session.installation,request.device_session.id))

    @extend_schema(request=ResolvePriceConflictInput,responses=ProductPriceConflictSerializer)
    def resolve(self,request,*args,**kwargs):
        from catalogue.services.price_conflicts import resolve_price_conflict
        self.context()
        incoming=ResolvePriceConflictInput(data=request.data);incoming.is_valid(raise_exception=True)
        result,_created=resolve_price_conflict(self.kwargs['shop_id'],self.kwargs['product_id'],self.kwargs['pk'],
            request.user,request.device_session.installation,incoming.validated_data,request.device_session.id)
        return Response(result)
