"""Manager-only transfer facts and paginated progress for each authorized shop."""
from django.db import transaction
from django.db.models import F, Q
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import serializers, viewsets
from rest_framework.response import Response

from common.api.fields import ExactIntegerField, StrictBoolean, StrictSerializer
from common.services.parsers import BoundedJSONParser
from common.services.origins import OriginReferenceConflict, with_public_origin
from purchasing.api.purchases import EventSerializer
from purchasing.api.suppliers import supplier_shop
from sales.services.pricing import MAX_FCFA
from stock.api.stock_cost_serializers import MovementCostSerializer
from stock.models import StockTransferReceipt
from stock.services.transfers import transfers_for, depart_transfer, receive_transfer, current_transfer


class DepartureLineInput(StrictSerializer):
    id=serializers.UUIDField()
    product_id=serializers.UUIDField()
    product_version=ExactIntegerField(min_value=1,max_value=MAX_FCFA)
    quantity_milli=ExactIntegerField(min_value=1,max_value=MAX_FCFA)


class ArrivalLineInput(StrictSerializer):
    id=serializers.UUIDField()
    transfer_line_id=serializers.UUIDField()
    product_id=serializers.UUIDField()
    quantity_milli=ExactIntegerField(min_value=1,max_value=MAX_FCFA)


class TransferInput(EventSerializer):
    destination_shop_id=serializers.UUIDField()
    is_departure_confirmed=StrictBoolean()
    lines=DepartureLineInput(many=True,allow_empty=False,max_length=500)

    def validate_lines(self,value):
        if len({row["id"] for row in value})!=len(value):raise serializers.ValidationError(_("Duplicate transfer line UUIDs."))
        return value


class ArrivalInput(EventSerializer):
    is_sellable_confirmed=StrictBoolean()
    lines=ArrivalLineInput(many=True,allow_empty=False,max_length=500)
    validate_lines=TransferInput.validate_lines


class TransferLineSerializer(serializers.Serializer):
    id=serializers.UUIDField()
    product_id=serializers.UUIDField()
    product_version=serializers.IntegerField()
    product_name=serializers.CharField()
    unit_kind=serializers.CharField()
    unit_label=serializers.CharField()
    quantity_milli=serializers.IntegerField()
    movement_id=serializers.UUIDField()
    received_milli=serializers.CharField()
    in_transit_milli=serializers.CharField()
    source_cost_revision_pending=serializers.BooleanField()
    cost=MovementCostSerializer(source="source_cost")


class TransferSerializer(serializers.Serializer):
    id=serializers.UUIDField()
    source_shop_id=serializers.UUIDField()
    destination_shop_id=serializers.UUIDField()
    operation_id=serializers.UUIDField()
    installation_id=serializers.UUIDField()
    actor_id=serializers.UUIDField(source="operation.actor_id")
    reason=serializers.CharField(source="operation.reason")
    occurred_at=serializers.DateTimeField()
    created_at=serializers.DateTimeField()
    lines=TransferLineSerializer(many=True)


class TransferReceiptLineSerializer(serializers.Serializer):
    id=serializers.UUIDField()
    transfer_line_id=serializers.UUIDField()
    product_id=serializers.UUIDField()
    product_version=serializers.IntegerField()
    product_name=serializers.CharField()
    unit_kind=serializers.CharField()
    unit_label=serializers.CharField()
    quantity_milli=serializers.IntegerField()
    received_before_milli=serializers.CharField()
    movement_id=serializers.UUIDField()
    cost_state=serializers.ChoiceField(choices=["known","provisional","unknown"])
    total_cost_nanos_fcfa=serializers.CharField(allow_null=True)


class TransferReceiptSerializer(serializers.Serializer):
    id=serializers.UUIDField()
    transfer_id=serializers.UUIDField()
    operation_id=serializers.UUIDField()
    installation_id=serializers.SerializerMethodField()
    actor_id=serializers.SerializerMethodField()
    reason=serializers.CharField(source="operation.reason")
    occurred_at=serializers.DateTimeField()
    created_at=serializers.DateTimeField()
    lines=TransferReceiptLineSerializer(many=True)

    def to_representation(self,instance):
        if not hasattr(instance,"public_actor_id") or not hasattr(instance,"public_installation_id"):
            # Unit acknowledgements use one scoped join; list rows are annotated.
            rows=StockTransferReceipt.objects.filter(id=instance.id).annotate(
                shop_id=F("transfer__destination_shop_id")).filter(operation__shop_id=F("shop_id")).filter(
                    Q(operation__recovered_origin__isnull=True)
                    | Q(operation__recovered_origin__capture__shop_id=F("shop_id")))
            identity=rows.annotate(
                public_actor_id=Coalesce(F("operation__recovered_origin__capture__origin_user_id"),F("operation__actor_id")),
                public_installation_id=Coalesce(F("operation__recovered_origin__capture__origin_installation_id"),F("installation_id"))
            ).values("public_actor_id","public_installation_id").first()
            if identity is None:raise OriginReferenceConflict()
            instance.public_actor_id=identity["public_actor_id"]
            instance.public_installation_id=identity["public_installation_id"]
        return super().to_representation(instance)

    @extend_schema_field(serializers.UUIDField())
    def get_actor_id(self,obj):return str(obj.public_actor_id)

    @extend_schema_field(serializers.UUIDField())
    def get_installation_id(self,obj):return str(obj.public_installation_id)


ERRORS={code:{"type":"object","additionalProperties":True} for code in (400,401,403,404,409,415)}


class StockTransferViewSet(viewsets.GenericViewSet):
    queryset=None
    serializer_class=TransferSerializer
    parser_classes=[BoundedJSONParser]

    @extend_schema(responses={200:TransferSerializer(many=True),**ERRORS},description=(
        "Current responsible lists transfers involving this authorized shop. Stable SQL pages, 25 default/100 maximum. "
        "Each source line reports shipped, physically received and remaining in-transit quantities separately. "
        "source_cost_revision_pending flags later source cost revisions without silently rewriting the frozen cost."))
    @transaction.atomic
    def list(self,request,*args,**kwargs):
        shop=supplier_shop(kwargs["shop_id"],request)
        page=self.paginate_queryset(transfers_for(shop))
        return self.get_paginated_response(TransferSerializer(page,many=True).data)

    @extend_schema(responses={200:TransferSerializer,**ERRORS},description=(
        "Read the current progress of one transfer after current shop authorization. Incoming access does not authorize "
        "the source catalogue or another shop. No cash or financial obligation is created by this transfer."))
    @transaction.atomic
    def retrieve(self,request,*args,**kwargs):
        shop=supplier_shop(kwargs["shop_id"],request)
        return Response(TransferSerializer(current_transfer(shop,kwargs["pk"])).data)

    @extend_schema(request=TransferInput,responses={200:TransferSerializer,201:TransferSerializer,**ERRORS},description=(
        "Confirm an actual physical departure from the current responsible's source shop to a different active shop "
        "of the same owner. Source product versions, units, names and exact departure costs are frozen. Destination "
        "stock is unchanged. Original command receipts are returned after live authorization on exact retries; "
        "new content under the same UUID is refused. Unconfirmed departures and quantities exceeding known available "
        "stock are not applied; the latter case awaits its explicit business rule."))
    def create(self,request,*args,**kwargs):
        supplier_shop(kwargs["shop_id"],request)
        incoming=TransferInput(data=request.data);incoming.is_valid(raise_exception=True)
        receipt,created=depart_transfer(kwargs["shop_id"],request,incoming.validated_data)
        return Response(receipt,status=201 if created else 200)


class StockTransferReceiptViewSet(viewsets.GenericViewSet):
    queryset=None
    serializer_class=TransferReceiptSerializer
    parser_classes=[BoundedJSONParser]

    @extend_schema(responses={200:TransferReceiptSerializer(many=True),**ERRORS},description=(
        "Current responsible of either participating shop reads immutable physical receipt facts. SQL pagination "
        "25/100, effective date/UUID order. Each partial receipt keeps its explicitly chosen destination product."))
    @transaction.atomic
    def list(self,request,*args,**kwargs):
        shop=supplier_shop(kwargs["shop_id"],request)
        transfer=current_transfer(shop,kwargs["pk"])
        rows=StockTransferReceipt.objects.filter(transfer=transfer).annotate(shop_id=F("transfer__destination_shop_id"))
        rows=with_public_origin(rows,actor_path="operation__actor_id").select_related("operation").prefetch_related("lines").order_by("occurred_at","id")
        return self.get_paginated_response(TransferReceiptSerializer(self.paginate_queryset(rows),many=True).data)

    @extend_schema(request=ArrivalInput,responses={200:TransferReceiptSerializer,201:TransferReceiptSerializer,**ERRORS},description=(
        "Only the current destination responsible confirms physically received sellable units. Each partial receipt "
        "explicitly chooses an existing destination product of the same frozen unit kind and base-unit label; no "
        "automatic catalogue match or conversion. Cumulative received quantity cannot exceed shipped quantity. "
        "Exact frozen costs are apportioned by cumulative half-up nano-FCFA differences, preserving their total "
        "across partial receipts to different products. No payment, debt or return to source is implied."))
    def create(self,request,*args,**kwargs):
        shop=supplier_shop(kwargs["shop_id"],request)
        get_object_or_404(transfers_for(shop),id=kwargs["pk"],destination_shop=shop)
        incoming=ArrivalInput(data=request.data);incoming.is_valid(raise_exception=True)
        receipt,created=receive_transfer(shop.id,kwargs["pk"],request,incoming.validated_data)
        return Response(receipt,status=201 if created else 200)
