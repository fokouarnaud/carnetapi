"""US-20 / G18: manager merge endpoint; the operation UUID makes retries return the original receipt."""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from common.api.fields import StrictSerializer
from common.models import Activity
from common.services.errors import OperationConflict
from common.services.operations import current_shop, require_manager
from customers.models import Customer
from customers.services.merges import merge_customers
from identity.services.authentication import require_live_session


class MergeInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    target_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000)


class MergeReceipt(serializers.Serializer):
    id = serializers.UUIDField()
    merged_into = serializers.UUIDField()
    version = serializers.IntegerField()


class CustomerMergeView(APIView):
    @extend_schema(request=MergeInput, responses={200: MergeReceipt, 201: MergeReceipt})
    @transaction.atomic
    def post(self, request, shop_id, client_id):
        session = request.device_session
        require_live_session(request.user, session.id, session.installation)
        shop = current_shop(shop_id, request.user, session.installation)
        require_manager(shop, request.user)
        serializer = MergeInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        existing = Activity.objects.filter(id=data["operation_id"]).first()
        if existing is not None:
            if (existing.action != "client_merged" or existing.shop_id != shop.id or existing.target_id != client_id
                    or existing.details.get("merged_into") != str(data["target_id"])):
                raise OperationConflict()
            return Response(existing.details["response"])
        source = get_object_or_404(Customer, shop=shop, id=client_id)
        target = get_object_or_404(Customer, shop=shop, id=data["target_id"])
        try:
            merged = merge_customers(source, target, actor=request.user, reason=data["reason"], operation_id=data["operation_id"])
        except DjangoValidationError as error:
            raise serializers.ValidationError({"reason": error.messages})
        receipt = {"id": str(merged.id), "merged_into": str(target.id), "version": merged.version}
        event = Activity.objects.get(id=data["operation_id"])
        event.details["response"] = receipt
        event.save(update_fields=["details", "updated_at"])
        return Response(receipt, status=201)
