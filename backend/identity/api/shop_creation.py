"""Create a shop for its authenticated owner, with an immutable operation receipt."""
from django.db import transaction
from rest_framework import serializers
from identity.services.authentication import require_live_session
from common.api.fields import StrictSerializer
from common.services.operations import current_shop, json_value, original_receipt
from common.services.errors import OperationConflict
from common.models import Activity
from identity.models import Shop
from identity.api.serializers import ShopSerializer


class CreateShopInput(StrictSerializer):
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    name = serializers.CharField(max_length=150, allow_blank=False)
    reason = serializers.CharField(max_length=2000, allow_blank=True, default="")


@transaction.atomic
def create_shop(actor, session, data):
    require_live_session(actor, session.id, session.installation)
    canonical = json_value({**data, "installation_id": session.installation_id})
    previous = Activity.objects.select_related("shop").filter(id=data["operation_id"]).first()
    if previous is not None:
        if previous.shop_id != data["id"]:
            raise OperationConflict()
        receipt = original_receipt(data["operation_id"], previous.shop, actor, "shop_created", canonical)
        current_shop(previous.shop_id, actor, session.installation)
        return receipt, False
    if Shop.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    shop = Shop.objects.create(id=data["id"], owner=actor, name=data["name"])
    receipt = json_value(ShopSerializer(shop).data)
    Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor, action="shop_created",
        target_id=shop.id, reason=data["reason"], details={"request": canonical, "response": receipt,
            "installation_id": str(session.installation_id),
            "after": {**receipt, "owner_id": str(actor.id), "is_active": True}})
    return receipt, True
