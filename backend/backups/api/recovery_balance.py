"""Read current recovery metadata without granting business or closure authority."""
from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from backups.api.recovery_closures import TechnicalRecoveryView
from backups.api.recovery_devices import RecoveryDeviceAccessError
from backups.services.recovery_balance import (
    balance_origins, build_balance, shop_balance_context, technical_balance_context,
)
from common.services.pagination import BoundedPagination
from identity.services.authentication import DeviceJWTAuthentication


class RecoveryBalanceCountsSerializer(serializers.Serializer):
    captured = serializers.IntegerField(min_value=0)
    applied = serializers.IntegerField(min_value=0)
    already_known = serializers.IntegerField(min_value=0)
    pending = serializers.IntegerField(min_value=0)
    refused = serializers.IntegerField(min_value=0)
    without_decision = serializers.IntegerField(min_value=0)


class RecoveryBalanceOriginSerializer(serializers.Serializer):
    shop_id = serializers.UUIDField()
    origin_installation_id = serializers.UUIDField()
    origin_user_id = serializers.UUIDField()
    seen_in_archived_inventory = serializers.BooleanField()
    origin_identity_conflict = serializers.BooleanField()
    captures = RecoveryBalanceCountsSerializer()
    refused_reservations_remaining = serializers.IntegerField(min_value=0)
    journal_complete = serializers.BooleanField(allow_null=True)
    coverage_since_snapshot = serializers.BooleanField(allow_null=True)


class RecoveryBalanceSummarySerializer(serializers.Serializer):
    session_id = serializers.UUIDField()
    generation_id = serializers.UUIDField()
    shop_id = serializers.UUIDField(allow_null=True)
    snapshot_at = serializers.DateTimeField(allow_null=True)
    inventory_recorded_at = serializers.DateTimeField(allow_null=True)
    archived_device_observations = serializers.IntegerField(min_value=0, allow_null=True)
    origin_count = serializers.IntegerField(min_value=0)
    origins_outside_archived_inventory = serializers.IntegerField(min_value=0)
    captures = RecoveryBalanceCountsSerializer()
    refused_reservations_remaining = serializers.IntegerField(min_value=0)
    classification_basis = serializers.CharField()
    inventory_exhaustive = serializers.BooleanField(allow_null=True)
    evidence_gaps = serializers.ListField(child=serializers.CharField())
    can_close_complete = serializers.BooleanField()
    exhaustive_recovery_guaranteed = serializers.BooleanField()
    closure_id = serializers.UUIDField(allow_null=True)


class RecoveryBalancePageSerializer(serializers.Serializer):
    count = serializers.IntegerField(min_value=0)
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = serializers.ListField(child=RecoveryBalanceOriginSerializer(), max_length=100)
    summary = RecoveryBalanceSummarySerializer()


PAGE_PARAMETERS = [OpenApiParameter("page", OpenApiTypes.INT, description="One-based page."),
    OpenApiParameter("page_size", OpenApiTypes.INT, description="25 by default; at most 100 origins returned.")]
RESPONSES = {200: RecoveryBalancePageSerializer,
    401: OpenApiResponse(RecoveryDeviceAccessError, description="Current authentication is missing, expired or revoked."),
    403: OpenApiResponse(RecoveryDeviceAccessError, description="Current responsible or technical role required for this route."),
    404: OpenApiResponse(RecoveryDeviceAccessError, description="Current recovery session or shop scope is inaccessible or unknown.")}
DESCRIPTION = (
    "Read only metadata; no source payload, money, costs, decision request/receipt or Activity details are loaded. "
    "The SQL union contains archived observations and discovered (shop, installation, user) origins, ordered by those UUIDs. "
    "Totals cover the full authorized filter, independently of pagination. Each capture has one disposition from its latest "
    "recorded integration decision, otherwise the latest matching source review, otherwise without_decision. "
    "An Activity UUID alone never establishes already_known. Exact pending/refused source reviews survive generations "
    "only with matching operation UUID, shop and fingerprint. Remaining refused reservations are counted individually, "
    "without adding quantities of different units. Null journal/retention/exhaustiveness evidence means unknown. "
    "No attestation, business effect, right, closure decision or reopening is created; can_close_complete remains false."
)


class _BalanceResponse:
    def balance_response(self, registry, *, shop_id=None):
        paginator = BoundedPagination()
        origins = paginator.paginate_queryset(balance_origins(registry, shop_id=shop_id), self.request, view=self)
        result = build_balance(registry, origins, shop_id=shop_id)
        response = paginator.get_paginated_response(result["results"])
        response.data["summary"] = result["summary"]
        response["Cache-Control"] = "private, no-store"
        return response


class ShopRecoveryBalanceView(_BalanceResponse, APIView):
    authentication_classes = [DeviceJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(parameters=[*PAGE_PARAMETERS, OpenApiParameter("shop_id", OpenApiTypes.UUID,
        location=OpenApiParameter.PATH, required=True)], responses=RESPONSES,
        description="Current confirmed shop responsible only; authorization precedes filtering and pagination. " + DESCRIPTION)
    @transaction.atomic
    def get(self, request, shop_id):
        registry, scoped_shop = shop_balance_context(shop_id=shop_id, actor=request.user,
            session_id=request.device_session.id)
        return self.balance_response(registry, shop_id=scoped_shop)


class TechnicalRecoveryBalanceView(_BalanceResponse, TechnicalRecoveryView):
    @extend_schema(parameters=[*PAGE_PARAMETERS, OpenApiParameter("session_id", OpenApiTypes.UUID,
        location=OpenApiParameter.PATH, required=True)], responses=RESPONSES,
        description="Current technical superadministrator inspects global operational metadata without a business membership. " + DESCRIPTION)
    @transaction.atomic
    def get(self, request, session_id):
        registry = technical_balance_context(actor=request.user, restore_session_id=session_id,
            session=getattr(request, "device_session", None))
        return self.balance_response(registry)
