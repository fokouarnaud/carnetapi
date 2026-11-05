"""Current managers match refused departures with a concrete before/after preview."""
from django.db import transaction
from drf_spectacular.utils import extend_schema, extend_schema_serializer
from rest_framework import serializers, viewsets
from rest_framework.response import Response
from backups.api.recovery_integration import ERRORS
from backups.api.recovery_journals import RecoveryJournalParser
from backups.api.recovery_metadata import RecoveryPagination
from backups.services.recovery_reservation_reconciliation import (
    ReconciliationInput, preview_reconciliation, reconcile_reservation, reconciliation_context,
)
from backups.services.recovery_sale_holds import hold_context
from sales.services.pricing import MAX_FCFA


class ReconciliationQuery(serializers.Serializer):
    movement_id=serializers.UUIDField()
    quantity_milli=serializers.IntegerField(min_value=1,max_value=MAX_FCFA)


@extend_schema_serializer(many=False)
class ReconciliationPreview(serializers.Serializer):
    capture_id=serializers.UUIDField()
    hold_id=serializers.UUIDField()
    reservation_id=serializers.UUIDField()
    source_operation_id=serializers.UUIDField()
    product_id=serializers.UUIDField()
    movement_id=serializers.UUIDField()
    quantity_milli=serializers.IntegerField(min_value=1)
    reservation_original_milli=serializers.CharField()
    reservation_reconciled_milli=serializers.CharField()
    reservation_remaining_milli=serializers.CharField()
    movement_accounted_milli=serializers.CharField()
    movement_claimed_milli=serializers.CharField()
    movement_remaining_milli=serializers.CharField()
    validated_before_milli=serializers.CharField()
    validated_after_milli=serializers.CharField()
    reserved_before_milli=serializers.CharField()
    reserved_after_milli=serializers.CharField()
    available_before_milli=serializers.CharField()
    available_after_milli=serializers.CharField()
    can_apply=serializers.BooleanField()
    issues=serializers.ListField(child=serializers.ChoiceField(choices=["reservation_not_refused",
        "movement_not_validated_negative_loss_or_inventory","reservation_quantity_exceeded",
        "accounted_quantity_exceeded","piece_requires_whole_quantity"]))
    preview_token=serializers.CharField()


@extend_schema_serializer(many=False)
class ReconciliationReceipt(serializers.Serializer):
    id=serializers.UUIDField()
    operation_id=serializers.UUIDField()
    capture_id=serializers.UUIDField()
    reservation_id=serializers.UUIDField()
    movement_id=serializers.UUIDField()
    product_id=serializers.UUIDField()
    quantity_milli=serializers.IntegerField(min_value=1)
    source_operation_id=serializers.UUIDField()
    actor_id=serializers.UUIDField()
    installation_id=serializers.UUIDField()
    recorded_at=serializers.DateTimeField()
    reason=serializers.CharField()
    before_after=ReconciliationPreview()


class RecoveryReservationReconciliationViewSet(viewsets.GenericViewSet):
    queryset=None
    parser_classes=[RecoveryJournalParser]
    pagination_class=RecoveryPagination
    serializer_class=ReconciliationReceipt

    def arguments(self):
        self.shop,_live=hold_context(self.kwargs["shop_id"],self.request.user,self.request.device_session.id)
        return {"shop_id":self.shop.id,"actor":self.request.user,"session_id":self.request.device_session.id,
            "capture_id":self.kwargs["pk"],"reservation_id":self.kwargs["reservation_id"]}

    def get_shop(self):return self.shop

    @extend_schema(parameters=[ReconciliationQuery],responses={200:ReconciliationPreview,**ERRORS},description=(
        "Preview a current responsible's explicit match of a refused reservation to an already validated negative loss "
        "or inventory correction of the same shop/product. The quantity is bounded by the unresolved reservation and "
        "unused accounted movement, across all sources. Positive inventory revisions reduce the shared count capacity. "
        "Decimal-text totals preserve exact large quantities. No quantity, cash, sale or refund is created by preview."))
    def preview(self,request,*args,**kwargs):
        arguments=self.arguments()
        query=ReconciliationQuery(data=request.query_params);query.is_valid(raise_exception=True)
        return Response(preview_reconciliation(**arguments,data=query.validated_data))

    @extend_schema(request=ReconciliationInput,responses={200:ReconciliationReceipt,201:ReconciliationReceipt,**ERRORS},description=(
        "Confirm the exact preview with a required reason. A stale preview or excessive quantity returns 409 atomically. "
        "Only the matched reservation quantity is released; original movements, reservation, refusal and money remain. "
        "The sale remains refused. Same command/content returns its historical receipt after current authorization. "
        "Reconciliation does not create a physical movement, payment, sale or automatic financial regularization."))
    def create(self,request,*args,**kwargs):
        arguments=self.arguments()
        receipt,created=reconcile_reservation(**arguments,data=request.data)
        return Response(receipt,status=201 if created else 200)

    @extend_schema(responses={200:ReconciliationReceipt(many=True),**ERRORS},description=(
        "Read the reservation's immutable match receipts after current authorization and source-integrity verification. "
        "SQL pagination is 25 by default, at most 100, ordered by recording time and UUID. Historical receipts retain "
        "their original preview; obtain a new preview for current remaining quantities."))
    @transaction.atomic
    def list(self,request,*args,**kwargs):
        arguments=self.arguments()
        _shop,_live,_capture,_hold,reservation=reconciliation_context(**arguments)
        rows=reservation.reconciliations.select_related("operation").order_by("created_at","id")
        page=self.paginate_queryset(rows)
        return self.get_paginated_response([row.operation.details["response"] for row in page])
