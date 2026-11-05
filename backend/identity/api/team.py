"""Online, audited changes of existing memberships; no client supplied authority."""
from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.response import Response

from identity.services.access import effective_permissions
from identity.services.authentication import require_live_session
from common.api.fields import ExactIntegerField, StrictBoolean, StrictSerializer
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from common.models import Activity
from identity.models import Membership


PERMISSIONS = (
    "can_grant_credit", "can_collect_debt", "can_read_debt", "can_prepare_reminder",
    "can_read_cost", "can_read_cash_balance",
)
DEPENDENCIES = ("can_grant_credit", "can_collect_debt", "can_prepare_reminder")


class TeamConflict(APIException):
    status_code = 409
    default_code = "team_conflict"


class ChangeMembershipSerializer(StrictSerializer):
    operation_id = serializers.UUIDField()
    base_version = ExactIntegerField(min_value=1, max_value=9223372036854775806)
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    role = serializers.ChoiceField(choices=Membership.Role.choices, required=False)
    is_active = StrictBoolean(required=False)
    can_grant_credit = StrictBoolean(required=False)
    can_collect_debt = StrictBoolean(required=False)
    can_read_debt = StrictBoolean(required=False)
    can_prepare_reminder = StrictBoolean(required=False)
    can_read_cost = StrictBoolean(required=False)
    can_read_cash_balance = StrictBoolean(required=False)
    confirm_revoke_dependencies = StrictBoolean(required=False, default=False)

    def validate(self, data):
        if not set(data).intersection({*PERMISSIONS, "role", "is_active"}):
            raise serializers.ValidationError("Aucun changement de droits proposé.")
        return data


class MembershipSerializer(serializers.ModelSerializer):
    user_id = serializers.UUIDField(read_only=True)
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = Membership
        fields = ["id", "user_id", "role", "is_active", "version", "permissions"]

    @extend_schema_field(serializers.DictField(child=serializers.BooleanField()))
    def get_permissions(self, obj):
        if not obj.is_active:
            return dict.fromkeys(PERMISSIONS, False)
        return effective_permissions(obj.shop, obj.user)[1]


@transaction.atomic
def change_membership(shop_id, member_id, actor, installation, session_id, data):
    require_live_session(actor, session_id, installation)
    shop = current_shop(shop_id, actor, installation)
    require_manager(shop, actor)
    member = get_object_or_404(Membership.objects.select_related("shop", "user"), shop=shop, id=member_id)
    if member.user_id == shop.owner_id:
        raise PermissionDenied("La propriété ne se modifie pas par une appartenance.")
    # A manager cannot nominate, demote, remove or modify another manager.
    if actor.id != shop.owner_id and (member.role == "manager" or data.get("role") == "manager"):
        raise PermissionDenied("Nomination et gestion des responsables réservées au propriétaire.")
    canonical = json_value({**data, "membership_id": member_id})
    receipt = original_receipt(data["operation_id"], shop, actor, "membership_changed", canonical)
    if receipt is not None:
        return receipt
    if member.version != data["base_version"]:
        raise TeamConflict({"code": "stale_membership_version", "current_version": member.version})
    changes = {name: value for name, value in data.items() if name in {*PERMISSIONS, "role", "is_active"}}
    if changes.get("can_read_debt") is False:
        dependent = [name for name in DEPENDENCIES if changes.get(name, getattr(member, name))]
        if dependent and not data["confirm_revoke_dependencies"]:
            raise TeamConflict({"code": "dependent_permissions", "dependent_permissions": dependent})
        changes.update(dict.fromkeys(dependent, False))
    before = json_value(MembershipSerializer(member).data)
    for name, value in changes.items():
        setattr(member, name, value)
    member.version += 1
    member.save(update_fields=[*changes, "version", "updated_at"])
    receipt = json_value(MembershipSerializer(member).data)
    Activity.objects.create(
        id=data["operation_id"], shop=shop, actor=actor, action="membership_changed",
        target_id=member.id, reason=data["reason"],
        details={"request": canonical, "before": before, "after": receipt, "response": receipt},
    )
    return receipt


class MembershipHistorySerializer(serializers.ModelSerializer):
    actor_id = serializers.UUIDField(read_only=True)
    before = serializers.JSONField(source="details.before", read_only=True)
    after = serializers.JSONField(source="details.after", read_only=True)

    class Meta:
        model = Activity
        fields = ["id", "actor_id", "created_at", "action", "reason", "before", "after"]


class TeamViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = Membership.objects.none()
    serializer_class = MembershipSerializer

    def get_shop(self):
        shop = current_shop(self.kwargs["shop_id"], self.request.user, self.request.device_session.installation)
        require_manager(shop, self.request.user)
        return shop

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Membership.objects.none()
        return Membership.objects.filter(shop=self.get_shop()).select_related("shop", "user").order_by("created_at", "id")

    def retrieve(self, request, *args, **kwargs):
        return Response(self.get_serializer(self.get_object()).data)

    @extend_schema(responses=MembershipHistorySerializer(many=True))
    def history(self, request, *args, **kwargs):
        member = self.get_object()
        events = Activity.objects.filter(
            shop_id=member.shop_id, target_id=member.id, action__in=["membership_changed","restore_seller_access_decided"],
        ).order_by("created_at", "id")
        page = self.paginate_queryset(events)
        return self.get_paginated_response(MembershipHistorySerializer(page, many=True).data)

    @extend_schema(request=ChangeMembershipSerializer, responses=MembershipSerializer)
    def partial_update(self, request, *args, **kwargs):
        self.get_object()
        serializer = ChangeMembershipSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(change_membership(
            self.kwargs["shop_id"], self.kwargs["pk"], request.user,
            request.device_session.installation, request.device_session.id, serializer.validated_data,
        ))
