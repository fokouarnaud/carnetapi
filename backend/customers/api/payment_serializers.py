from rest_framework import serializers

from common.api.fields import ExactIntegerField, StrictSerializer
from customers.models import CustomerPayment, CustomerPaymentPart, DebtAllocation
from sales.services.pricing import MAX_FCFA
from sales.api.sale_serializers import CreateSalePaymentSerializer
from common.services.origins import public_identity
from drf_spectacular.utils import extend_schema_field


class CreateAllocationSerializer(StrictSerializer):
    id = serializers.UUIDField()
    debt_id = serializers.UUIDField()
    amount_fcfa = ExactIntegerField(min_value=1, max_value=MAX_FCFA)


class CreateCustomerPaymentSerializer(StrictSerializer):
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    parts = CreateSalePaymentSerializer(many=True, allow_empty=False, max_length=50)
    allocations = CreateAllocationSerializer(many=True, max_length=500)

    def validate(self, data):
        for name in ("parts", "allocations"):
            ids = [row["id"] for row in data[name]]
            if len(ids) != len(set(ids)):
                raise serializers.ValidationError({name: "UUID répété."})
        total = sum(row["amount_fcfa"] for row in data["parts"])
        if total > MAX_FCFA or sum(row["amount_fcfa"] for row in data["allocations"]) > total:
            raise serializers.ValidationError("Affectations ou montant total hors capacité du paiement.")
        return data


class CustomerPaymentPartSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomerPaymentPart
        fields = ["id", "method", "amount_fcfa", "other_name", "external_reference", "received_fcfa", "returned_fcfa", "possible_duplicate_reference"]
        read_only_fields = fields


class DebtAllocationSerializer(serializers.ModelSerializer):
    debt_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = DebtAllocation
        fields = ["id", "debt_id", "requested_amount_fcfa", "applied_amount_fcfa"]
        read_only_fields = fields


class CustomerPaymentSerializer(serializers.ModelSerializer):
    shop_id = serializers.UUIDField(read_only=True)
    customer_id = serializers.UUIDField(read_only=True)
    actor_id = serializers.SerializerMethodField()
    installation_id = serializers.SerializerMethodField()
    operation_id = serializers.UUIDField(read_only=True)
    parts = CustomerPaymentPartSerializer(many=True, read_only=True)
    allocations = DebtAllocationSerializer(many=True, read_only=True)

    @extend_schema_field(serializers.UUIDField())
    def get_actor_id(self,obj):return public_identity(obj,self.context,"actor_id")

    @extend_schema_field(serializers.UUIDField())
    def get_installation_id(self,obj):return public_identity(obj,self.context,"installation_id")

    class Meta:
        model = CustomerPayment
        fields = ["id", "shop_id", "customer_id", "actor_id", "installation_id", "operation_id", "occurred_at",
                  "amount_fcfa", "unallocated_fcfa", "requires_regularization", "parts", "allocations"]
        read_only_fields = fields


class QueryAmountField(serializers.IntegerField):
    def to_internal_value(self, data):
        if not isinstance(data, str) or not data.isascii() or not data.isdigit() or len(data) > 19:
            self.fail("invalid")
        return super().to_internal_value(data)


class PaymentPreviewRequestSerializer(StrictSerializer):
    amount_fcfa = QueryAmountField(min_value=1, max_value=MAX_FCFA)


class AllocationProposalSerializer(serializers.Serializer):
    debt_id = serializers.UUIDField()
    amount_fcfa = serializers.IntegerField()


class PaymentPreviewSerializer(serializers.Serializer):
    amount_fcfa = serializers.IntegerField()
    known_debt_fcfa = serializers.IntegerField()
    unallocated_fcfa = serializers.IntegerField()
    allocations = AllocationProposalSerializer(many=True)
    allocation_limit_reached = serializers.BooleanField()
