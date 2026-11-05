from django.shortcuts import get_object_or_404
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.response import Response

from identity.services.access import accessible_shops
from identity.services.authentication import require_live_session
from customers.api.client_serializers import (
    ClientHistorySerializer, ClientSerializer, CorrectionReceiptSerializer, CreateClientSerializer,
    ReportCorrectionSerializer, UpdateClientSerializer,
)
from common.services.operations import require_manager
from common.services.origins import with_public_origin
from common.api.errors import OriginReferenceError
from customers.services.client_services import create_client, report_correction, update_client
from customers.services.duplicate_queries import possible_duplicates_of, with_duplicate_hint
from common.models import Activity
from customers.models import Customer


class ClientViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = Customer.objects.none()
    serializer_class = ClientSerializer

    def get_shop(self):
        require_live_session(self.request.user, self.request.device_session.id, self.request.device_session.installation)
        return get_object_or_404(
            accessible_shops(self.request.user, self.request.device_session.installation),
            id=self.kwargs["shop_id"],
        )

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Customer.objects.none()
        return with_duplicate_hint(Customer.objects.filter(shop=self.get_shop())).order_by("created_at", "id")

    @extend_schema(responses=ClientSerializer(many=True))
    @transaction.atomic
    def possible_duplicates(self, request, *args, **kwargs):
        customer = self.get_object()
        page = self.paginate_queryset(possible_duplicates_of(customer))
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    @extend_schema(request=CreateClientSerializer, responses={200: ClientSerializer, 201: ClientSerializer})
    def create(self, request, *args, **kwargs):
        self.get_shop()
        serializer = CreateClientSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = create_client(
            self.kwargs["shop_id"], request.user, request.device_session.installation, serializer.validated_data,
            request.device_session.id,
        )
        return Response(receipt, status=201 if created else 200)

    @extend_schema(responses={200:ClientHistorySerializer(many=True),409:OriginReferenceError})
    def history(self, request, *args, **kwargs):
        customer = self.get_object()
        require_manager(self.get_shop(), request.user)
        events = Activity.objects.filter(
            shop_id=customer.shop_id, target_id=customer.id,
            action__in=["client_created", "client_updated", "client_correction_reported"],
        ).order_by("created_at", "id")
        page = self.paginate_queryset(with_public_origin(events,operation_path="",installation_path=None))
        return self.get_paginated_response(ClientHistorySerializer(page, many=True).data)

    @extend_schema(request=UpdateClientSerializer, responses=ClientSerializer)
    def partial_update(self, request, *args, **kwargs):
        self.get_object()
        require_manager(self.get_shop(), request.user)
        serializer = UpdateClientSerializer(data=request.data)
        # PATCH changes selected contact fields; operation/version/reason remain mandatory.
        serializer.is_valid(raise_exception=True)
        receipt = update_client(
            self.kwargs["shop_id"], self.kwargs["pk"], request.user,
            request.device_session.installation, serializer.validated_data, request.device_session.id,
        )
        return Response(receipt)

    @extend_schema(request=ReportCorrectionSerializer, responses={200: CorrectionReceiptSerializer, 201: CorrectionReceiptSerializer})
    def report_correction(self, request, *args, **kwargs):
        self.get_object()
        serializer = ReportCorrectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = report_correction(
            self.kwargs["shop_id"], self.kwargs["pk"], request.user,
            request.device_session.installation, serializer.validated_data, request.device_session.id,
        )
        return Response(receipt, status=201 if created else 200)
