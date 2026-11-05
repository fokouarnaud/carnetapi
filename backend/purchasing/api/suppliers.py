"""Shop-scoped supplier records under current manager permissions."""
from django.db import transaction
from django.core.validators import RegexValidator
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.response import Response

from identity.services.authentication import require_live_session
from common.api.fields import StrictSerializer
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from common.services.errors import OperationConflict
from common.models import Activity
from purchasing.models import Supplier


class CreateSupplierSerializer(StrictSerializer):
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    name = serializers.CharField(max_length=150, allow_blank=False)
    phone = serializers.CharField(max_length=16, allow_null=True, default=None,
                                   validators=[RegexValidator(r"^\+[1-9][0-9]{7,14}$")])


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = ["id", "name", "phone", "version"]
        read_only_fields = fields


def supplier_shop(shop_id, request):
    require_live_session(request.user, request.device_session.id, request.device_session.installation)
    shop = current_shop(shop_id, request.user, request.device_session.installation)
    require_manager(shop, request.user)
    return shop


@transaction.atomic
def create_supplier(shop_id, request, data):
    shop = supplier_shop(shop_id, request)
    canonical = json_value({**data, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "supplier_created", canonical)
    if original is not None:
        return original, False
    if Supplier.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    return _persist_supplier(shop=shop,actor=request.user,installation=request.device_session.installation,data=data,canonical=canonical)


def _persist_supplier(*,shop,actor,installation,data,canonical,origin_capture=None,source_snapshot=None,recovery_review=None):
    """Create only the supplier identity inside the caller's authorized transaction."""
    supplier = Supplier.objects.create(id=data["id"], shop=shop, name=data["name"], phone=data["phone"])
    receipt = json_value(SupplierSerializer(supplier).data)
    event=Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor, action="supplier_created",
        target_id=supplier.id, reason="Création du fournisseur", details={"request": canonical, "response": receipt})
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=event,capture=origin_capture)
        event.details['recovery']=json_value({'capture_id':origin_capture.id,'origin_user_id':origin_capture.origin_user_id,
            'origin_installation_id':origin_capture.origin_installation_id,'origin_recorded_at':origin_capture.origin_recorded_at,
            'source_occurred_at':origin_capture.occurred_at,'origin_installation_verified':False,
            'source_snapshot':source_snapshot,'review':recovery_review})
        event.save(update_fields=['details','updated_at'])
    return receipt, True


class SupplierViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = Supplier.objects.none()
    serializer_class = SupplierSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Supplier.objects.none()
        return Supplier.objects.filter(shop=supplier_shop(self.kwargs["shop_id"], self.request)).order_by("created_at", "id")

    @extend_schema(request=CreateSupplierSerializer, responses={200: SupplierSerializer, 201: SupplierSerializer})
    def create(self, request, *args, **kwargs):
        supplier_shop(self.kwargs["shop_id"], request)
        serializer = CreateSupplierSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = create_supplier(self.kwargs["shop_id"], request, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)
