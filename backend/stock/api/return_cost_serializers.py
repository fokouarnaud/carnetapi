"""Expose exact cost integers as strings so clients cannot lose large integer precision."""
from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from stock.services.return_costs import current_return_cost, integral_unit_cost


class ReturnCostSerializer(serializers.Serializer):
    receipt_cost_id = serializers.UUIDField(source="id", read_only=True)
    revision_id = serializers.UUIDField(read_only=True, allow_null=True)
    version = serializers.IntegerField(read_only=True)
    origin_kind = serializers.ChoiceField(choices=["frozen", "reconstructed_legacy"], read_only=True)
    sale_cost_id = serializers.UUIDField(read_only=True, allow_null=True)
    cost_state = serializers.ChoiceField(choices=["known", "provisional", "unknown"], read_only=True)
    total_cost_nanos_fcfa = serializers.CharField(read_only=True, allow_null=True)
    unit_cost_micros_numerator = serializers.CharField(read_only=True, allow_null=True)
    unit_cost_micros_denominator = serializers.CharField(read_only=True, allow_null=True)


def cost_representation(snapshot, *, total=None, override_total=False):
    if snapshot is None:
        return None
    is_revision = hasattr(snapshot, "origin_id")
    origin = snapshot.origin if is_revision else snapshot
    values = {"id": snapshot.origin_id if is_revision else snapshot.id,
        "revision_id": snapshot.id if is_revision else None, "version": snapshot.version if is_revision else 1,
        "origin_kind": "reconstructed_legacy" if getattr(origin, "reconstruction", None) is not None else "frozen",
        "sale_cost_id": snapshot.sale_cost_id, "cost_state": snapshot.cost_state,
        "total_cost_nanos_fcfa": total if override_total else snapshot.total_cost_nanos_fcfa,
        "unit_cost_micros_numerator": snapshot.unit_cost_micros_numerator,
        "unit_cost_micros_denominator": snapshot.unit_cost_micros_denominator}
    return ReturnCostSerializer(values).data


class ReceiptCostFields(serializers.Serializer):
    """Keep exact cost, legacy integer projection and uncertainty consistent."""
    unit_cost_micros = serializers.SerializerMethodField()
    cost_is_provisional = serializers.SerializerMethodField()

    def cost_receipt(self, obj):
        return obj

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_unit_cost_micros(self, obj):
        receipt = self.cost_receipt(obj)
        cost = current_return_cost(receipt)
        if cost is None:
            return receipt.unit_cost_micros
        return integral_unit_cost({"unit_cost_micros_numerator": cost.unit_cost_micros_numerator,
            "unit_cost_micros_denominator": cost.unit_cost_micros_denominator})

    def get_cost_is_provisional(self, obj) -> bool:
        receipt = self.cost_receipt(obj)
        cost = current_return_cost(receipt)
        return receipt.cost_is_provisional if cost is None else cost.cost_state != "known"
