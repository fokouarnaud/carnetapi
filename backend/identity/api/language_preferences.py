"""Persist only the authenticated account's language, without renewing shop grants."""
from django.db import transaction
from django.utils.translation import gettext_lazy
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from identity.services.authentication import require_live_session
from common.api.fields import ExactIntegerField, StrictSerializer
from common.services.operations import json_value
from common.services.errors import OperationConflict
from identity.models import LanguagePreferenceChange, User


class LanguagePreferenceInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    base_version = ExactIntegerField(min_value=1, max_value=9223372036854775806)
    language = serializers.ChoiceField(choices=["fr", "en"])


class LanguagePreferenceSerializer(serializers.Serializer):
    user_id = serializers.UUIDField(read_only=True)
    language = serializers.CharField(read_only=True)
    version = serializers.IntegerField(read_only=True)


class LanguageVersionConflict(APIException):
    status_code = 409
    default_detail = gettext_lazy("La préférence de langue a changé. Relire sa version.")
    default_code = "language_version_conflict"


def current_account(request):
    require_live_session(request.user, request.device_session.id, request.device_session.installation)
    return User.objects.get(id=request.user.id)


def account_receipt(user):
    return {"user_id": str(user.id), "language": user.language, "version": user.language_version}


class LanguagePreferenceViewSet(viewsets.GenericViewSet):
    queryset = User.objects.none()
    serializer_class = LanguagePreferenceSerializer

    @extend_schema(responses=LanguagePreferenceSerializer)
    @transaction.atomic
    def retrieve(self, request, *args, **kwargs):
        return Response(account_receipt(current_account(request)))

    @extend_schema(request=LanguagePreferenceInput, responses=LanguagePreferenceSerializer)
    @transaction.atomic
    def partial_update(self, request, *args, **kwargs):
        user = current_account(request)
        serializer = LanguagePreferenceInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        canonical = json_value(data)
        existing = LanguagePreferenceChange.objects.filter(id=data["operation_id"]).first()
        if existing is not None:
            if existing.user_id != user.id or existing.request != canonical:
                raise OperationConflict()
            return Response(existing.response)
        if user.language_version != data["base_version"]:
            raise LanguageVersionConflict()
        before = user.language
        user.language = data["language"]
        user.language_version += 1
        user.save(update_fields=["language", "language_version"])
        receipt = account_receipt(user)
        LanguagePreferenceChange.objects.create(id=data["operation_id"], user=user,
            before_language=before, after_language=user.language, version=user.language_version,
            request=canonical, response=receipt)
        return Response(receipt)
