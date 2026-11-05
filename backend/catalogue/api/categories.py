"""Shop-authorized category identities with durable creation receipts."""
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, viewsets
from rest_framework.response import Response
from identity.services.authentication import require_live_session
from common.api.fields import ExactIntegerField, StrictSerializer
from rest_framework.exceptions import APIException
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from common.services.errors import OperationConflict
from common.models import Activity
from catalogue.models import Category


class CategoryVersionConflict(APIException):
    status_code = 409
    default_code = "category_version_conflict"
    default_detail = "La catégorie a été modifiée. Relire sa version avant correction."


class CategoryInput(StrictSerializer):
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    name = serializers.CharField(max_length=150, allow_blank=False)


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name", "version"]
        read_only_fields = fields


class CategoryUpdateInput(StrictSerializer):
    operation_id = serializers.UUIDField()
    base_version = ExactIntegerField(min_value=1, max_value=9223372036854775806)
    name = serializers.CharField(max_length=150, allow_blank=False)
    reason = serializers.CharField(max_length=2000, allow_blank=False)


def category_shop(shop_id, request):
    require_live_session(request.user, request.device_session.id, request.device_session.installation)
    return current_shop(shop_id, request.user, request.device_session.installation)


@transaction.atomic
def create_category(shop_id, request, data):
    shop = category_shop(shop_id, request)
    require_manager(shop, request.user)
    canonical = json_value({**data, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "category_created", canonical)
    if original is not None:
        return original, False
    if Category.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    category = Category.objects.create(id=data["id"], shop=shop, name=data["name"])
    receipt = json_value(CategorySerializer(category).data)
    Activity.objects.create(id=data["operation_id"], shop=shop, actor=request.user, action="category_created",
                            target_id=category.id, reason="Création de catégorie", details={"request": canonical, "response": receipt})
    return receipt, True


class CategoryViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = Category.objects.none()
    serializer_class = CategorySerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        return Category.objects.filter(shop=category_shop(self.kwargs["shop_id"], self.request)).order_by("created_at", "id")

    @extend_schema(request=CategoryInput, responses={200: CategorySerializer, 201: CategorySerializer})
    def create(self, request, *args, **kwargs):
        require_manager(category_shop(self.kwargs["shop_id"], request), request.user)
        serializer = CategoryInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = create_category(self.kwargs["shop_id"], request, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)

    @extend_schema(request=CategoryUpdateInput, responses=CategorySerializer)
    @transaction.atomic
    def partial_update(self, request, *args, **kwargs):
        category = self.get_object()
        require_manager(category.shop, request.user)
        serializer = CategoryUpdateInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        canonical = json_value({**data, "category_id": category.id, "installation_id": request.device_session.installation.id})
        original = original_receipt(data["operation_id"], category.shop, request.user, "category_updated", canonical)
        if original is not None:
            return Response(original)
        if category.version != data["base_version"]:
            raise CategoryVersionConflict()
        before = json_value(CategorySerializer(category).data)
        category.name = data["name"]
        category.version += 1
        category.save(update_fields=["name", "version", "updated_at"])
        receipt = json_value(CategorySerializer(category).data)
        Activity.objects.create(id=data["operation_id"], shop=category.shop, actor=request.user, action="category_updated",
                                target_id=category.id, reason=data["reason"], details={"request": canonical, "before": before, "after": receipt, "response": receipt})
        return Response(receipt)
