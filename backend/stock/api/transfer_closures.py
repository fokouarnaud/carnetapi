"""US-16 / G16: the destination manager closes the whole in-transit remainder of a line; nothing returns to the source."""
from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from common.api.fields import StrictSerializer
from common.models import Activity
from common.services.errors import OperationConflict
from purchasing.api.suppliers import supplier_shop
from stock.models import StockTransfer, StockTransferLineClosure
from stock.services.transfers import transfer_lines


class TransferClosureInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    outcome = serializers.ChoiceField(choices=["lost", "damaged"])
    reason = serializers.CharField(max_length=2000)


class TransferClosureReceipt(serializers.Serializer):
    id = serializers.UUIDField()
    line_id = serializers.UUIDField()
    outcome = serializers.CharField()
    quantity_milli = serializers.IntegerField()


class TransferLineClosureView(APIView):
    @extend_schema(request=TransferClosureInput, responses={200: TransferClosureReceipt, 201: TransferClosureReceipt})
    @transaction.atomic
    def post(self, request, shop_id, transfer_id, line_id):
        shop = supplier_shop(shop_id, request)
        serializer = TransferClosureInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        existing = Activity.objects.filter(id=data["operation_id"]).first()
        if existing is not None:
            if existing.action != "stock_transfer_remainder_closed" or existing.shop_id != shop.id or existing.target_id != line_id:
                raise OperationConflict()
            return Response(existing.details["response"])
        transfer = get_object_or_404(StockTransfer, id=transfer_id, destination_shop=shop)
        line = get_object_or_404(transfer_lines().filter(transfer=transfer), id=line_id)
        remaining = int(line.in_transit_milli)
        if remaining <= 0 or StockTransferLineClosure.objects.filter(line_id=line.id).exists():
            raise OperationConflict()
        if not data["reason"].strip():
            raise serializers.ValidationError({"reason": "Motif obligatoire."})
        event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=request.user, action="stock_transfer_remainder_closed",
                                        target_id=line.id, reason=data["reason"].strip(), details={})
        closure = StockTransferLineClosure.objects.create(line_id=line.id, operation=event, installation=request.device_session.installation,
                                                          quantity_milli=remaining, outcome=data["outcome"], reason=data["reason"].strip())
        receipt = {"id": str(closure.id), "line_id": str(line.id), "outcome": closure.outcome, "quantity_milli": remaining}
        event.details["response"] = receipt
        event.save(update_fields=["details", "updated_at"])
        return Response(receipt, status=201)
