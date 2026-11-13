"""US-37 / G19: explicit release of a refused reservation whose goods are still present; no stock movement."""
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from common.api.fields import ExactIntegerField, StrictSerializer
from common.models import Activity
from common.services.errors import OperationConflict
from common.services.operations import current_shop, require_manager
from identity.services.authentication import require_live_session
from stock.models import StockReservation, StockReservationPresenceRelease, StockReservationReconciliation


class PresenceReleaseInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    quantity_milli = ExactIntegerField(min_value=1, max_value=9223372036854775807)
    reason = serializers.CharField(max_length=2000)


class PresenceReleaseReceipt(serializers.Serializer):
    id = serializers.UUIDField()
    reservation_id = serializers.UUIDField()
    quantity_milli = serializers.IntegerField()
    remaining_reserved_milli = serializers.IntegerField()


def remaining_reserved(reservation):
    used = sum(model.objects.filter(reservation=reservation).aggregate(total=Sum("quantity_milli"))["total"] or 0
               for model in (StockReservationReconciliation, StockReservationPresenceRelease))
    return reservation.quantity_milli - used


class PresenceReleaseView(APIView):
    @extend_schema(request=PresenceReleaseInput, responses={200: PresenceReleaseReceipt, 201: PresenceReleaseReceipt})
    @transaction.atomic
    def post(self, request, shop_id, reservation_id):
        session = request.device_session
        require_live_session(request.user, session.id, session.installation)
        shop = current_shop(shop_id, request.user, session.installation)
        require_manager(shop, request.user)
        serializer = PresenceReleaseInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        existing = Activity.objects.filter(id=data["operation_id"]).first()
        if existing is not None:
            if existing.action != "reservation_presence_released" or existing.shop_id != shop.id or existing.target_id != reservation_id:
                raise OperationConflict()
            return Response(existing.details["response"])
        reservation = get_object_or_404(StockReservation.objects.select_for_update(), id=reservation_id, product__shop=shop)
        if reservation.state != "rejected":
            raise serializers.ValidationError({"reservation": "Seule une réservation refusée peut être libérée."})
        if not data["reason"].strip() or data["quantity_milli"] > remaining_reserved(reservation):
            raise serializers.ValidationError({"quantity_milli": "Quantité supérieure au reliquat réservé ou motif vide."})
        event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=request.user, action="reservation_presence_released",
                                        target_id=reservation.id, reason=data["reason"].strip(), details={})
        release = StockReservationPresenceRelease.objects.create(reservation=reservation, operation=event, installation=session.installation,
                                                                 quantity_milli=data["quantity_milli"], reason=data["reason"].strip())
        receipt = {"id": str(release.id), "reservation_id": str(reservation.id), "quantity_milli": release.quantity_milli,
                   "remaining_reserved_milli": remaining_reserved(reservation)}
        event.details["response"] = receipt
        event.save(update_fields=["details", "updated_at"])
        return Response(receipt, status=201)
