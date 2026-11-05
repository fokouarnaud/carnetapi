from django.db import transaction
from rest_framework import serializers
from rest_framework.exceptions import APIException

from identity.services.authentication import require_live_session
from common.api.fields import ExactIntegerField, StrictSerializer
from common.services.operations import current_shop, json_value, original_receipt, require_manager
from common.models import Activity


class DiscountPolicySerializer(serializers.Serializer):
    shop_id = serializers.UUIDField()
    seller_discount_cap_basis_points = serializers.IntegerField(min_value=0, max_value=10000)
    version = serializers.IntegerField(min_value=1)


class ChangeDiscountPolicySerializer(StrictSerializer):
    operation_id = serializers.UUIDField()
    base_version = ExactIntegerField(min_value=1, max_value=9223372036854775806)
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    seller_discount_cap_basis_points = ExactIntegerField(min_value=0, max_value=10000)


class DiscountPolicyConflict(APIException):
    status_code = 409
    default_code = "discount_policy_conflict"
    default_detail = "Le plafond a changé. Relire sa version avant modification."


def policy_response(shop):
    return DiscountPolicySerializer({
        "shop_id": shop.id, "seller_discount_cap_basis_points": shop.seller_discount_cap_basis_points,
        "version": shop.discount_policy_version,
    }).data


@transaction.atomic
def change_discount_policy(shop_id, actor, installation, session_id, data):
    require_live_session(actor, session_id, installation)
    shop = current_shop(shop_id, actor, installation)
    require_manager(shop, actor)
    canonical = json_value(data)
    original = original_receipt(data["operation_id"], shop, actor, "discount_policy_changed", canonical)
    if original is not None:
        return original
    if shop.discount_policy_version != data["base_version"]:
        raise DiscountPolicyConflict()
    before = json_value(policy_response(shop))
    shop.seller_discount_cap_basis_points = data["seller_discount_cap_basis_points"]
    shop.discount_policy_version += 1
    shop.save(update_fields=["seller_discount_cap_basis_points", "discount_policy_version", "updated_at"])
    receipt = json_value(policy_response(shop))
    Activity.objects.create(
        id=data["operation_id"], shop=shop, actor=actor, action="discount_policy_changed", target_id=shop.id,
        reason=data["reason"], details={"request": canonical, "before": before, "after": receipt, "response": receipt},
    )
    return receipt
