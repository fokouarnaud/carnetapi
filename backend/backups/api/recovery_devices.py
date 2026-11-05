"""Read current recovery observations without granting identity or access."""
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema,extend_schema_field,OpenApiResponse
from rest_framework import mixins,serializers,viewsets
from rest_framework.exceptions import PermissionDenied

from backups.models import RecoveryDevice
from backups.services.recovery_status import recovery_status_for_shop
from backups.api.recovery_metadata import RecoveryPagination,RECOVERY_SCHEMA
from common.services.operations import current_shop,require_manager
from identity.services.authentication import require_live_session


class RecoveryDeviceSerializer(serializers.ModelSerializer):
    session_id=serializers.UUIDField(read_only=True)
    shop_id=serializers.UUIDField(read_only=True)
    recorded_at=serializers.DateTimeField(source="created_at",read_only=True,
        help_text="Observation copied into the registry; never a phone contact or recovery completion date.")

    class Meta:
        model=RecoveryDevice
        fields=["id","session_id","shop_id","origin_installation_id","origin_user_id","label","recorded_at"]
        read_only_fields=fields


class RecoveryDeviceDetailSerializer(RecoveryDeviceSerializer):
    restoration=serializers.SerializerMethodField()

    @extend_schema_field(RECOVERY_SCHEMA)
    def get_restoration(self,device):
        return self.context["restoration"]

    class Meta(RecoveryDeviceSerializer.Meta):
        fields=RecoveryDeviceSerializer.Meta.fields+["restoration"]
        read_only_fields=fields


class RecoveryDeviceAccessError(serializers.Serializer):
    code=serializers.CharField()
    detail=serializers.CharField()


class RecoveryInspectionDenied(PermissionDenied):
    default_detail=_("Only a current shop responsible can inspect recovery devices.")


ERRORS={401:OpenApiResponse(RecoveryDeviceAccessError,description="Missing, expired, revoked or inactive current session/account."),
    403:OpenApiResponse(RecoveryDeviceAccessError,description="The current shop role is not responsible; seller permissions do not grant inspection."),
    404:OpenApiResponse(RecoveryDeviceAccessError,description="Shop or observation is inaccessible, historical, or belongs to another shop.")}


class RecoveryDeviceViewSet(mixins.ListModelMixin,mixins.RetrieveModelMixin,viewsets.GenericViewSet):
    queryset=RecoveryDevice.objects.none()
    serializer_class=RecoveryDeviceSerializer
    pagination_class=RecoveryPagination

    def get_shop(self):
        if not hasattr(self,"_shop"):
            session=self.request.device_session
            require_live_session(self.request.user,session.id,session.installation)
            shop=current_shop(self.kwargs["shop_id"],self.request.user,session.installation)
            try:
                require_manager(shop,self.request.user)
            except PermissionDenied:
                raise RecoveryInspectionDenied() from None
            self._shop=shop
        return self._shop

    def get_queryset(self):
        if getattr(self,"swagger_fake_view",False):
            return RecoveryDevice.objects.none()
        return RecoveryDevice.objects.filter(shop=self.get_shop(),session__generation__current_slot=1).order_by("created_at","id")

    def get_serializer_class(self):
        return RecoveryDeviceDetailSerializer if self.action=="retrieve" else RecoveryDeviceSerializer

    def get_serializer_context(self):
        context=super().get_serializer_context()
        if self.action=="retrieve" and not getattr(self,"swagger_fake_view",False):
            context["restoration"]=recovery_status_for_shop(self.get_shop())
        return context

    @extend_schema(responses={200:RecoveryDeviceSerializer(many=True),**ERRORS},description=(
        "A current shop responsible inspects only the current restoration's device observations. "
        "Authorization precedes SQL filtering and bounded pagination (25 by default, maximum 100); order is created_at then UUID. "
        "A technical account without a business membership has no implicit access. "
        "The restoration metadata distinguishes an unrecorded inventory (known_devices null) from zero observations. "
        "Neither an observation nor an empty page proves recovery, identity verification or current rights. "
        "GET only; creation, editing and deletion are not supported. No offline lease is renewed."))
    @transaction.atomic
    def list(self,request,*args,**kwargs):
        return super().list(request,*args,**kwargs)

    @extend_schema(responses={200:RecoveryDeviceDetailSerializer,**ERRORS},description=(
        "Read one observation only after current responsible/session and shop authorization. "
        "An origin UUID is historical data, not a current account or installation grant. "
        "Foreign and historical observations return the same generic 404 as an unknown UUID. GET only."))
    @transaction.atomic
    def retrieve(self,request,*args,**kwargs):
        return super().retrieve(request,*args,**kwargs)
