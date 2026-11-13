"""US-28 / G17: the manager explicitly closes an invoice line's unreceived remainder; debt and stock are unchanged."""
from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from common.api.fields import StrictSerializer
from common.models import Activity
from common.services.errors import OperationConflict
from purchasing.api.purchases import context, receipt_document
from purchasing.models import PurchaseLine, PurchaseLineClosure


class LineClosureInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000)


class LineClosureReceipt(serializers.Serializer):
    id = serializers.UUIDField()
    line_id = serializers.UUIDField()
    quantity_milli = serializers.IntegerField()


class PurchaseLineClosureView(APIView):
    @extend_schema(request=LineClosureInput, responses={200: LineClosureReceipt, 201: LineClosureReceipt})
    @transaction.atomic
    def post(self, request, shop_id, supplier_id, document_id, line_id):
        shop, supplier = context(shop_id, supplier_id, request)
        serializer = LineClosureInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        existing = Activity.objects.filter(id=data["operation_id"]).first()
        if existing is not None:
            if existing.action != "purchase_remainder_closed" or existing.shop_id != shop.id or existing.target_id != line_id:
                raise OperationConflict()
            return Response(existing.details["response"])
        document = receipt_document(shop, supplier, document_id)
        line = get_object_or_404(PurchaseLine, id=line_id, document=document)
        remaining = line.quantity_milli - sum(part.quantity_milli for part in line.receipt_lines.all())
        if remaining <= 0 or PurchaseLineClosure.objects.filter(line=line).exists():
            raise OperationConflict()
        if not data["reason"].strip():
            raise serializers.ValidationError({"reason": "Motif obligatoire."})
        event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=request.user, action="purchase_remainder_closed",
                                        target_id=line.id, reason=data["reason"].strip(), details={})
        closure = PurchaseLineClosure.objects.create(line=line, operation=event, installation=request.device_session.installation,
                                                     quantity_milli=remaining, reason=data["reason"].strip())
        receipt = {"id": str(closure.id), "line_id": str(line.id), "quantity_milli": remaining}
        event.details["response"] = receipt
        event.save(update_fields=["details", "updated_at"])
        return Response(receipt, status=201)
