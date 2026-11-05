"""Bounded journal capture and responsible-only inspection during recovery."""
import json

from django.db import transaction
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema,extend_schema_serializer,OpenApiResponse
from rest_framework import mixins,serializers,viewsets
from rest_framework.exceptions import ParseError
from rest_framework.parsers import BaseParser
from rest_framework.response import Response

from backups.api.recovery_devices import RecoveryDeviceAccessError
from backups.api.recovery_metadata import RecoveryPagination
from backups.journal_protocol import JournalDepositInput,MAX_JOURNAL_BYTES,canonical_bytes
from backups.models import RecoveryJournalDeposit,RecoveryJournalOperation
from backups.services.recovery_journal import journal_context,capture_recovery_journal


def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError()
        result[key]=value
    return result


def _invalid_constant(value):raise ValueError()


class RecoveryJournalParser(BaseParser):
    media_type="application/json"

    def parse(self,stream,media_type=None,parser_context=None):
        try:
            request=parser_context["request"]
            length=int(request.META.get("CONTENT_LENGTH") or 0)
            if not 0<=length<=MAX_JOURNAL_BYTES:raise ValueError()
            raw=stream.read(MAX_JOURNAL_BYTES+1)
            if len(raw)>MAX_JOURNAL_BYTES:raise ValueError()
            decoded=json.loads(raw.decode("utf-8"),object_pairs_hook=_unique_object,parse_constant=_invalid_constant)
            # Reject unpaired Unicode even in unknown keys before error rendering.
            canonical_bytes(decoded)
            return decoded
        except (ValueError,TypeError,UnicodeError,RecursionError):
            raise ParseError(_("Invalid or oversized recovery journal.")) from None


class JournalCapturedReference(serializers.Serializer):
    operation_id=serializers.UUIDField()
    capture_id=serializers.UUIDField()


@extend_schema_serializer(many=False)
class JournalReceiptSerializer(serializers.Serializer):
    id=serializers.UUIDField()
    operation_id=serializers.UUIDField()
    session_id=serializers.UUIDField()
    shop_id=serializers.UUIDField()
    submitted_by=serializers.UUIDField()
    submitted_installation_id=serializers.UUIDField()
    reason=serializers.CharField()
    received_at=serializers.DateTimeField()
    format_version=serializers.IntegerField()
    capture_state=serializers.ChoiceField(choices=["captured"])
    operation_count=serializers.IntegerField(min_value=1,max_value=50)
    new_operation_count=serializers.IntegerField(min_value=0,max_value=50)
    business_effects_created=serializers.IntegerField(min_value=0,max_value=0,
        help_text="Effects created by this deposit, not a claim that the source operation is absent from the restored business database.")
    operations=JournalCapturedReference(many=True)


class JournalSummarySerializer(serializers.ModelSerializer):
    session_id=serializers.UUIDField(read_only=True)
    shop_id=serializers.UUIDField(read_only=True)
    submitted_by=serializers.UUIDField(source="submitted_by_id",read_only=True)
    submitted_installation_id=serializers.UUIDField(read_only=True)
    received_at=serializers.DateTimeField(source="created_at",read_only=True)

    class Meta:
        model=RecoveryJournalDeposit
        fields=["id","operation_id","session_id","shop_id","submitted_by","submitted_installation_id","reason","received_at","format_version"]
        read_only_fields=fields


class CapturedOperationSerializer(serializers.ModelSerializer):
    session_id=serializers.UUIDField(read_only=True)
    shop_id=serializers.UUIDField(read_only=True)
    first_deposit_id=serializers.UUIDField(read_only=True)
    dependencies=serializers.ListField(child=serializers.UUIDField(),read_only=True)

    class Meta:
        model=RecoveryJournalOperation
        fields=["id","operation_id","session_id","shop_id","first_deposit_id","origin_installation_id","origin_user_id",
            "operation_type","occurred_at","origin_recorded_at","created_at","payload","dependencies","content_sha256"]
        read_only_fields=fields


ERRORS={401:OpenApiResponse(RecoveryDeviceAccessError,description="Current authentication or installation session is missing, expired or revoked."),
    403:OpenApiResponse(RecoveryDeviceAccessError,description="Current shop responsible role required."),
    404:OpenApiResponse(RecoveryDeviceAccessError,description="Shop, deposit or capture is inaccessible, historical or unknown.")}
WRITE_ERRORS={**ERRORS,
    400:OpenApiResponse({"type":"object","additionalProperties":True},description="Invalid version, structure, bounds, JSON, credentials, duplicate UUIDs or destination shop; no capture committed."),
    409:OpenApiResponse(RecoveryDeviceAccessError,description="Current recovery session changed, or a deposit/source UUID is reused with different content or origin; nothing overwritten."),
    415:OpenApiResponse(RecoveryDeviceAccessError,description="application/json required.")}


class RecoveryJournalScope(viewsets.GenericViewSet):
    pagination_class=RecoveryPagination

    def get_shop(self):
        if not hasattr(self,"_shop"):
            self._shop,_=journal_context(self.kwargs["shop_id"],self.request.user,self.request.device_session.id)
        return self._shop

    def get_queryset(self):
        if getattr(self,"swagger_fake_view",False):return self.queryset.none()
        return self.queryset.filter(shop=self.get_shop(),session__generation__current_slot=1).order_by("created_at","id")

class RecoveryJournalViewSet(mixins.ListModelMixin,RecoveryJournalScope):
    queryset=RecoveryJournalDeposit.objects.all()
    serializer_class=JournalSummarySerializer
    parser_classes=[RecoveryJournalParser]

    @extend_schema(responses={200:JournalSummarySerializer(many=True),**ERRORS},description=(
        "Current responsible inspects only this shop's current recovery deposits. Authorization precedes SQL pagination, "
        "25 rows by default, maximum 100, order received time then UUID. Capturing a copy never grants origin rights or proves completeness."))
    @transaction.atomic
    def list(self,request,*args,**kwargs):return super().list(request,*args,**kwargs)

    @extend_schema(request=JournalDepositInput,responses={200:JournalReceiptSerializer,201:JournalReceiptSerializer,**WRITE_ERRORS},description=(
        "Current authorized responsible deposits surviving source operations, including copies from withdrawn accounts/installations. "
        "A live current session and shop role are checked before parsing and again in the write transaction. "
        "Version 1: 1 to 50 operations, at most 2000000 request bytes and 1000000 canonical UTF-8 bytes per payload, JSON depth 20, "
        "100 dependency UUIDs per operation. Explicit UTC offsets required. Duplicate JSON keys, credentials and repeated source UUIDs in a batch are rejected. "
        "The entire batch and receipt commit atomically. Same command/content returns the original receipt; another deposit recognizes source UUIDs "
        "in the same recovery session. Changed source content or origin is 409, without overwriting or partial capture. "
        "Dependencies and origin UUIDs are declared data; missing accounts/operations are not invented. "
        "No sale, payment, stock movement, reservation, access grant or offline lease is created. Receipt means captured, not business validation or recovery closure."))
    def create(self,request,*args,**kwargs):
        self.get_shop()
        receipt,created=capture_recovery_journal(shop_id=self.kwargs["shop_id"],actor=request.user,
            session_id=request.device_session.id,data=request.data)
        return Response(receipt,status=201 if created else 200)

    @extend_schema(responses={200:JournalReceiptSerializer,**ERRORS},description=(
        "Read the original durable capture receipt after current responsible and shop authorization. Current recovery generation only; "
        "foreign/historical/unknown UUIDs share the same 404. The receipt is historical technical evidence, not current business validation."))
    @transaction.atomic
    def retrieve(self,request,*args,**kwargs):return Response(self.get_object().receipt)


class RecoveryOperationViewSet(mixins.ListModelMixin,mixins.RetrieveModelMixin,RecoveryJournalScope):
    queryset=RecoveryJournalOperation.objects.all()
    serializer_class=CapturedOperationSerializer

    @extend_schema(responses={200:CapturedOperationSerializer(many=True),**ERRORS},description=(
        "Current responsible reads captured source content for this shop's current recovery, before business integration. "
        "Opaque payload and declared origins confer no rights or proof. SQL pagination 25/default, 100/max, created time then UUID; "
        "restoration metadata covers the authorized shop. No operation is applied or dependency fabricated by inspection."))
    @transaction.atomic
    def list(self,request,*args,**kwargs):return super().list(request,*args,**kwargs)

    @extend_schema(responses={200:CapturedOperationSerializer,**ERRORS},description=(
        "Read one captured source operation after current responsible/shop authorization. "
        "Historical and foreign captures return generic 404; this is source evidence, not an integration decision."))
    @transaction.atomic
    def retrieve(self,request,*args,**kwargs):return super().retrieve(request,*args,**kwargs)
