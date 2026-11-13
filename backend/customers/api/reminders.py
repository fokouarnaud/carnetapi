"""US-25: prepare, preview, list and declare-sent customer reminders; text is never sent by the server."""
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from common.api.fields import StrictSerializer
from common.services.operations import current_shop
from common.services.pagination import BoundedPagination
from customers.models import Customer, CustomerReminder
from customers.services.reminders import (ReminderRefused, declare_sent, draft, prepare_reminder, receipt,
                                          require_reminder_permission)
from identity.services.authentication import require_live_session

REFUSALS = {
    "reminder_empty": status.HTTP_400_BAD_REQUEST,
    "reminder_debt_unknown": status.HTTP_400_BAD_REQUEST,
    "reminder_blocked_pending_payment": status.HTTP_409_CONFLICT,
}


class PrepareReminderInput(StrictSerializer):
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    language = serializers.ChoiceField(choices=["fr", "en"], default="fr")
    extra_debt_ids = serializers.ListField(child=serializers.UUIDField(), max_length=200, default=list)


class PreviewQuery(serializers.Serializer):
    language = serializers.ChoiceField(choices=["fr", "en"], default="fr")
    extra_debt_ids = serializers.CharField(required=False, default="")

    def validate_extra_debt_ids(self, value):
        field = serializers.UUIDField()
        return [field.to_internal_value(part) for part in value.split(",") if part.strip()][:200]


class DeclareSentInput(StrictSerializer):
    operation_id = serializers.UUIDField()


class ReminderDebtOutput(serializers.Serializer):
    debt_id = serializers.UUIDField()
    remaining_fcfa = serializers.IntegerField()
    overdue = serializers.BooleanField()


class ReminderPreviewOutput(serializers.Serializer):
    customer_id = serializers.UUIDField()
    language = serializers.CharField()
    phone = serializers.CharField(allow_null=True)
    message = serializers.CharField()
    total_fcfa = serializers.IntegerField()
    debts = ReminderDebtOutput(many=True)
    other_devices_warning = serializers.BooleanField()


class ReminderOutput(serializers.Serializer):
    id = serializers.UUIDField()
    customer_id = serializers.UUIDField()
    actor_id = serializers.UUIDField()
    language = serializers.CharField()
    phone = serializers.CharField(allow_null=True)
    message = serializers.CharField()
    total_fcfa = serializers.IntegerField()
    debts = ReminderDebtOutput(many=True)
    status = serializers.ChoiceField(choices=CustomerReminder.Status.choices)
    created_at = serializers.DateTimeField()
    declared_at = serializers.DateTimeField(allow_null=True)
    declared_by = serializers.UUIDField(allow_null=True)
    other_devices_warning = serializers.BooleanField()


def _context(request, shop_id):
    session = request.device_session
    require_live_session(request.user, session.id, session.installation)
    shop = current_shop(shop_id, request.user, session.installation)
    require_reminder_permission(shop, request.user)
    return shop, session.installation


def _refused(error):
    return Response({"code": error.code}, status=REFUSALS[error.code])


class CustomerReminderView(APIView):
    @extend_schema(responses=ReminderOutput(many=True))
    def get(self, request, shop_id, client_id):
        shop, _installation = _context(request, shop_id)
        customer = get_object_or_404(Customer, shop=shop, id=client_id)
        reminders = CustomerReminder.objects.filter(shop=shop, customer=customer).order_by("-created_at", "-id")
        paginator = BoundedPagination()
        page = paginator.paginate_queryset(reminders, request, view=self)
        return paginator.get_paginated_response([receipt(reminder) for reminder in page])

    @extend_schema(request=PrepareReminderInput, responses={200: ReminderOutput, 201: ReminderOutput})
    def post(self, request, shop_id, client_id):
        shop, installation = _context(request, shop_id)
        customer = get_object_or_404(Customer, shop=shop, id=client_id)
        data = PrepareReminderInput(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            response, created = prepare_reminder(shop, customer, request.user, installation, data.validated_data)
        except ReminderRefused as error:
            return _refused(error)
        return Response(response, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class CustomerReminderPreviewView(APIView):
    @extend_schema(parameters=[OpenApiParameter("language", str), OpenApiParameter("extra_debt_ids", str)],
                   responses=ReminderPreviewOutput)
    def get(self, request, shop_id, client_id):
        shop, _installation = _context(request, shop_id)
        customer = get_object_or_404(Customer, shop=shop, id=client_id)
        query = PreviewQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        try:
            content = draft(shop, customer, query.validated_data["extra_debt_ids"], query.validated_data["language"])
        except ReminderRefused as error:
            return _refused(error)
        return Response(content)


class DeclareReminderSentView(APIView):
    @extend_schema(request=DeclareSentInput, responses={200: ReminderOutput})
    def post(self, request, shop_id, pk):
        shop, _installation = _context(request, shop_id)
        reminder = get_object_or_404(CustomerReminder, shop=shop, id=pk)
        data = DeclareSentInput(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(declare_sent(reminder, request.user, data.validated_data["operation_id"]))
