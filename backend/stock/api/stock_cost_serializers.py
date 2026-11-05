"""Serialize exact stock costs without rounding or JSON integer precision loss."""
from rest_framework import serializers
from drf_spectacular.utils import extend_schema_field
from stock.models import StockCostSnapshot


class MovementCostSerializer(serializers.ModelSerializer):
    valuation_kind = serializers.SerializerMethodField()
    trigger_operation_id = serializers.UUIDField(read_only=True)
    supersedes_id = serializers.UUIDField(read_only=True, allow_null=True)
    total_cost_nanos_fcfa = serializers.CharField(read_only=True, allow_null=True)
    unit_cost_micros_numerator = serializers.CharField(read_only=True, allow_null=True)
    unit_cost_micros_denominator = serializers.CharField(read_only=True, allow_null=True)
    quantity_after_milli = serializers.CharField(read_only=True)
    stock_value_after_nanos_fcfa = serializers.CharField(read_only=True, allow_null=True)
    valuation_requires_regularization = serializers.BooleanField(read_only=True, allow_null=True)
    valuation_gap_nanos_fcfa = serializers.CharField(read_only=True, allow_null=True)

    @extend_schema_field(serializers.ChoiceField(choices=["captured", "reconstructed", "revised"]))
    def get_valuation_kind(self, obj):
        if obj.version > 1:
            return "revised"
        return "captured" if obj.trigger_operation_id == obj.movement.operation_id else "reconstructed"

    class Meta:
        model = StockCostSnapshot
        fields = ["id", "version", "valuation_kind", "trigger_operation_id", "supersedes_id", "created_at",
            "cost_state", "total_cost_nanos_fcfa", "unit_cost_micros_numerator",
            "unit_cost_micros_denominator", "quantity_after_milli", "stock_value_after_nanos_fcfa",
            "valuation_requires_regularization", "valuation_gap_nanos_fcfa"]
        read_only_fields = fields
