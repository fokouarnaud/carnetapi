"""Public physical-arrival receipt with the retained source cost evidence."""
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from purchasing.models import PurchaseReceipt,PurchaseReceiptLine


class ReceiptChargeAllocationSerializer(serializers.Serializer):
    allocation_id=serializers.UUIDField()
    revision_id=serializers.UUIDField()
    charge_id=serializers.UUIDField()
    amount_fcfa=serializers.IntegerField()


class ReceiptCostBasisSerializer(serializers.Serializer):
    invoice_total_fcfa=serializers.IntegerField()
    invoice_base_quantity_milli=serializers.IntegerField()
    received_before_base_milli=serializers.IntegerField()
    total_cost_nanos_fcfa=serializers.CharField()
    charge_allocations=ReceiptChargeAllocationSerializer(many=True)


class ReceiptLineSerializer(serializers.ModelSerializer):
    line_id=serializers.UUIDField(read_only=True)
    movement_id=serializers.UUIDField(read_only=True)
    product_id=serializers.UUIDField(source='line.product_id',read_only=True)
    unit_kind=serializers.SerializerMethodField()
    cost_basis=serializers.SerializerMethodField()

    def _proof(self,obj):
        return obj.receipt.operation.details.get('receipt_proofs',{}).get(str(obj.id),{})

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_unit_kind(self,obj):return self._proof(obj).get('unit_kind')

    @extend_schema_field(ReceiptCostBasisSerializer(allow_null=True))
    def get_cost_basis(self,obj):return self._proof(obj).get('cost_basis')

    class Meta:
        model=PurchaseReceiptLine
        fields=['id','line_id','movement_id','product_id','unit_kind','quantity_milli','base_quantity_milli','cost_basis']
        read_only_fields=fields


class ReceiptSerializer(serializers.ModelSerializer):
    lines=ReceiptLineSerializer(many=True,read_only=True)
    document_id=serializers.UUIDField(read_only=True)

    class Meta:
        model=PurchaseReceipt
        fields=['id','document_id','occurred_at','created_at','lines']
        read_only_fields=fields
