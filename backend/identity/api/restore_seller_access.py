"""Connected seller revalidation; archived permissions are metadata, never defaults."""
from drf_spectacular.utils import extend_schema,OpenApiResponse
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView
from common.api.fields import StrictSerializer,StrictBoolean,ExactIntegerField
from common.services.operations import current_shop,require_manager
from common.services.parsers import BoundedJSONParser
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from identity.models import Membership
from identity.services.restore_seller_access import inspect_seller_access,revalidate_seller_access


class SellerPermissions(StrictSerializer):
    can_grant_credit=StrictBoolean()
    can_collect_debt=StrictBoolean()
    can_read_debt=StrictBoolean()
    can_prepare_reminder=StrictBoolean()
    can_read_cost=StrictBoolean()
    can_read_cash_balance=StrictBoolean()


class SellerAccessInput(StrictSerializer):
    operation_id=serializers.UUIDField()
    generation_id=serializers.UUIDField()
    previous_review_version=ExactIntegerField(min_value=0,max_value=9223372036854775806)
    membership_version=ExactIntegerField(min_value=1,max_value=9223372036854775806)
    state=serializers.ChoiceField(choices=("confirmed","refused"))
    reason=serializers.CharField(max_length=2000,allow_blank=False)
    evidence_ref=serializers.CharField(max_length=500,required=False,default="",allow_blank=True)
    permissions=SellerPermissions(required=False)

    def validate(self,data):
        if (data["state"]=="confirmed")!=("permissions" in data):
            raise serializers.ValidationError(_("A confirmed decision requires a complete permission profile; refusal must not contain one."))
        return data


class RestoredMembershipOutput(serializers.Serializer):
    id=serializers.UUIDField()
    user_id=serializers.UUIDField()
    role=serializers.ChoiceField(choices=Membership.Role.choices)
    is_active=serializers.BooleanField()
    version=ExactIntegerField()
    account_active=serializers.BooleanField()
    declared_permissions=SellerPermissions()
    permissions=SellerPermissions()


class SellerAccessStatus(serializers.Serializer):
    review_id=serializers.UUIDField()
    generation_id=serializers.UUIDField()
    archived_role=serializers.ChoiceField(choices=("owner","manager","seller"))
    archived_permissions=SellerPermissions()
    state=serializers.ChoiceField(choices=("pending","confirmed","refused"))
    version=ExactIntegerField()
    membership=RestoredMembershipOutput()


class SellerAccessReceipt(serializers.Serializer):
    review_id=serializers.UUIDField()
    generation_id=serializers.UUIDField()
    decision_id=serializers.UUIDField()
    operation_id=serializers.UUIDField()
    state=serializers.ChoiceField(choices=("confirmed","refused"))
    version=ExactIntegerField()
    membership=RestoredMembershipOutput()


class SellerAccessError(serializers.Serializer):
    code=serializers.CharField()
    detail=serializers.JSONField()


ERRORS={400:OpenApiResponse({"oneOf":[{"type":"object","additionalProperties":{}},
    {"type":"array","items":{"type":"string"}}]},description="Translated field-error object or domain-message list: invalid fields, incomplete/non-native boolean profile, or a current role other than seller."),
    401:OpenApiResponse(SellerAccessError,description="Authentication or current installation/session is invalid."),
    403:OpenApiResponse(SellerAccessError,description="Current responsible rights or current account check is required."),
    404:OpenApiResponse(SellerAccessError,description="Shop, membership or current scoped restoration review is inaccessible."),
    409:OpenApiResponse(SellerAccessError,description="Stale generation/versions or operation UUID/content conflict."),
    415:OpenApiResponse(SellerAccessError,description="This endpoint accepts JSON only.")}


class RestoreSellerAccessView(APIView):
    parser_classes=[BoundedJSONParser]

    def arguments(self,request):
        return {"shop_id":self.kwargs["shop_id"],"member_id":self.kwargs["pk"],"actor":request.user,
            "installation":request.device_session.installation,"session_id":request.device_session.id}

    def authorize_scope(self,request):
        shop=current_shop(self.kwargs["shop_id"],request.user,request.device_session.installation)
        require_manager(shop,request.user)
        get_object_or_404(Membership,shop=shop,id=self.kwargs["pk"])

    @extend_schema(responses={200:SellerAccessStatus,**ERRORS},description=(
        "Read the current restoration review after responsible authorization. Archived and declared flags are metadata; "
        "permissions contain effective rights, all false while access/account/membership is suspended. No offline lease is renewed."))
    def get(self,request,**kwargs):
        return Response(inspect_seller_access(**self.arguments(request)))

    @extend_schema(request=SellerAccessInput,responses={200:OpenApiResponse(SellerAccessReceipt,description="Historical receipt replay, no second effect."),
        201:OpenApiResponse(SellerAccessReceipt,description="Current seller decision recorded atomically with its activity journal."),**ERRORS},
        description=("A current responsible revalidates a current seller in this shop with explicit fresh declared permissions. "
        "Confirmation requires all six native booleans; refusal excludes permissions. Actor authority and live session are rechecked "
        "inside the transaction, including before replay. No archived device/session is restored, no OTP or token is issued. "
        "Debt actions confer necessary debt reading even when autonomous reading is false. Refusal never deactivates the global account."))
    def post(self,request,**kwargs):
        self.authorize_scope(request)
        serializer=SellerAccessInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        result,created=revalidate_seller_access(**self.arguments(request),data=serializer.validated_data)
        return Response(result,status=201 if created else 200)
