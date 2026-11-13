"""US-07 / G22: technical superadministrator endpoints for the exceptional recovery case."""
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied, ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from common.api.fields import StrictSerializer
from identity.models import AccountRecoveryCase, User
from identity.services.account_recovery import complete_recovery_case, open_recovery_case
from identity.services.authentication import require_live_session


class OpenRecoveryInput(StrictSerializer):
    account_id = serializers.UUIDField()
    new_phone = serializers.RegexField(r"^\+[1-9][0-9]{7,14}$")
    identity_reference = serializers.CharField(max_length=120)
    facts_checked = serializers.IntegerField(min_value=0, max_value=100)
    confirming_member_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000)


class RecoveryCaseSerializer(serializers.ModelSerializer):
    state = serializers.SerializerMethodField()

    def get_state(self, obj) -> str:
        return "completed" if obj.completed_at else "waiting"

    class Meta:
        model = AccountRecoveryCase
        fields = ["id", "account", "new_phone", "facts_checked", "effective_after", "completed_at", "state"]


def technical_actor(request):
    session = request.device_session
    require_live_session(request.user, session.id, session.installation)
    if not request.user.is_superuser:
        raise PermissionDenied("Recours exceptionnel réservé au superadministrateur technique.")
    return request.user


def call(service, *args, **kwargs):
    try:
        return service(*args, **kwargs)
    except DjangoPermissionDenied as error:
        raise PermissionDenied(str(error))
    except DjangoValidationError as error:
        raise serializers.ValidationError({"detail": error.messages})


class RecoveryCaseListView(APIView):
    @extend_schema(request=OpenRecoveryInput, responses={201: RecoveryCaseSerializer})
    def post(self, request):
        actor = technical_actor(request)
        serializer = OpenRecoveryInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        account = get_object_or_404(User, id=data["account_id"])
        member = get_object_or_404(User, id=data["confirming_member_id"])
        case = call(open_recovery_case, account, data["new_phone"], actor=actor, identity_reference=data["identity_reference"],
                    facts_checked=data["facts_checked"], confirming_member=member, reason=data["reason"])
        return Response(RecoveryCaseSerializer(case).data, status=201)


class RecoveryCaseCompleteView(APIView):
    @extend_schema(request=None, responses={200: RecoveryCaseSerializer})
    def post(self, request, pk):
        actor = technical_actor(request)
        case = get_object_or_404(AccountRecoveryCase, id=pk)
        return Response(RecoveryCaseSerializer(call(complete_recovery_case, case, actor=actor)).data)


class OwnershipTransferInput(StrictSerializer):
    new_owner_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=2000)
    proofs = serializers.DictField()


class OwnershipTransferReceipt(serializers.Serializer):
    id = serializers.UUIDField()
    shop_id = serializers.UUIDField()
    previous_owner_id = serializers.UUIDField()
    new_owner_id = serializers.UUIDField()


class OwnershipTransferView(APIView):
    """US-47 / G24: superadmin-only reassignment of a shop, with reason and G22 proofs."""
    @extend_schema(request=OwnershipTransferInput, responses={201: OwnershipTransferReceipt})
    def post(self, request, shop_id):
        from identity.models import Shop
        from identity.services.ownership import transfer_ownership
        actor = technical_actor(request)
        serializer = OwnershipTransferInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        shop = get_object_or_404(Shop, id=shop_id)
        new_owner = get_object_or_404(User, id=data["new_owner_id"])
        record = call(transfer_ownership, shop, new_owner, actor=actor, reason=data["reason"], proofs=data["proofs"])
        return Response({"id": record.id, "shop_id": shop.id, "previous_owner_id": record.previous_owner_id,
                         "new_owner_id": record.new_owner_id}, status=201)
