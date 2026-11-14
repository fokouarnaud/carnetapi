from datetime import timedelta
from rest_framework import serializers
from django.utils import timezone
from django.utils.translation import gettext_lazy

from common.api.fields import ExactIntegerField, StrictSerializer
from common.services.device_clock import DEVICE_CLOCK_TOLERANCE
from customers.api.debts import CustomerDebtSerializer
from customers.models import CustomerCredit
from sales.models import Sale, SaleLine, SalePayment, SaleCancellation
from sales.services.pricing import MAX_FCFA
from common.services.origins import public_identity
from drf_spectacular.utils import extend_schema_field


class APIConflictSerializer(serializers.Serializer):
    detail = serializers.CharField()


class DebtConfirmationSerializer(serializers.Serializer):
    code = serializers.CharField()
    known_debt_fcfa = serializers.IntegerField(min_value=0)


class CreateSaleLineSerializer(StrictSerializer):
    id = serializers.UUIDField()
    product_id = serializers.UUIDField()
    product_version = ExactIntegerField(min_value=1, max_value=MAX_FCFA)
    pack_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    quantity_milli = ExactIntegerField(min_value=1, max_value=MAX_FCFA)
    own_discount_fcfa = ExactIntegerField(min_value=0, max_value=MAX_FCFA, default=0)


class DeclaredPaymentSerializer(StrictSerializer):
    id = serializers.UUIDField()
    method = serializers.ChoiceField(choices=SalePayment.Method.choices)
    amount_fcfa = ExactIntegerField(min_value=1, max_value=MAX_FCFA)
    other_name = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")
    external_reference = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")

    def validate(self, data):
        if data["method"] == "other" and not data["other_name"]:
            raise serializers.ValidationError({"other_name": "Nom du moyen obligatoire."})
        if data["method"] != "other" and data["other_name"]:
            raise serializers.ValidationError({"other_name": "Nom réservé au moyen autre."})
        return data


class CreateSalePaymentSerializer(DeclaredPaymentSerializer):
    received_fcfa = ExactIntegerField(min_value=1, max_value=MAX_FCFA, required=False)
    returned_fcfa = ExactIntegerField(min_value=0, max_value=MAX_FCFA, required=False)

    def validate(self, data):
        data = super().validate(data)
        has_received = "received_fcfa" in data
        has_returned = "returned_fcfa" in data
        if has_received != has_returned:
            raise serializers.ValidationError("Renseigner ensemble les montants remis et rendus.")
        if has_received and data["received_fcfa"] - data["returned_fcfa"] != data["amount_fcfa"]:
            raise serializers.ValidationError("Le net conservé doit égaler le remis moins le rendu.")
        return data


OFFLINE_LEASE = timedelta(days=7)


class CreateSaleSerializer(StrictSerializer):
    occurred_at = serializers.DateTimeField(required=False)
    # Q136 / decision of 13 November 2026: time recorded by the selling device while offline,
    # trusted only within the offline lease length. Not a manual backdate (G15).
    recorded_at = serializers.DateTimeField(required=False)
    reason = serializers.CharField(max_length=2000, allow_blank=False, required=False)
    backdate_reason = serializers.CharField(max_length=2000, allow_blank=False, required=False)  # G15, beyond 30 days
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    lines = CreateSaleLineSerializer(many=True, allow_empty=False, max_length=500)
    payments = CreateSalePaymentSerializer(many=True, max_length=50)
    global_discount_fcfa = ExactIntegerField(min_value=0, max_value=MAX_FCFA, default=0)
    mode = serializers.ChoiceField(choices=["cash", "credit"], default="cash")
    customer_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    due_date = serializers.DateField(required=False, allow_null=True, default=None)
    confirmed_debt_fcfa = ExactIntegerField(min_value=0, required=False, allow_null=True, default=None)

    def validate_occurred_at(self, value):
        if value > timezone.now():
            raise serializers.ValidationError(gettext_lazy("Une vente ne peut pas être datée dans le futur."))
        return value

    def validate_recorded_at(self, value):
        now = timezone.now()
        if value > now + DEVICE_CLOCK_TOLERANCE or value < now - OFFLINE_LEASE:
            raise serializers.ValidationError(
                gettext_lazy("Heure de l'appareil hors de la période hors connexion autorisée."),
                code="recorded_at_outside_lease")
        return value

    def validate(self, data):
        if "recorded_at" in data and "occurred_at" in data:
            raise serializers.ValidationError(
                {"recorded_at": gettext_lazy("Heure de l'appareil incompatible avec une date effective manuelle.")})
        if "occurred_at" in data and "reason" not in data:
            raise serializers.ValidationError({"reason": gettext_lazy("Motif obligatoire pour une date effective manuelle.")})
        if "reason" in data and "occurred_at" not in data:
            raise serializers.ValidationError({"occurred_at": gettext_lazy("Renseigner la date effective avec le motif.")})
        if data["mode"] == "cash" and data["due_date"] is not None:
            raise serializers.ValidationError({"due_date": "Échéance réservée aux ventes à crédit."})
        for name in ("lines", "payments"):
            ids = [row["id"] for row in data[name]]
            if len(ids) != len(set(ids)):
                raise serializers.ValidationError({name: "UUID répété dans la demande."})
        return data


class SaleLineSerializer(serializers.ModelSerializer):
    product_id = serializers.UUIDField(read_only=True)
    pack_id = serializers.UUIDField(read_only=True, allow_null=True)
    class Meta:
        model = SaleLine
        fields = ["id", "product_id", "product_version", "pack_id", "product_name", "unit_kind", "unit_label",
                  "quantity_milli", "factor_milli", "base_quantity_milli", "unit_price_fcfa", "reference_fcfa",
                  "own_discount_fcfa", "global_discount_fcfa", "net_fcfa"]
        read_only_fields = fields


class SalePaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SalePayment
        fields = ["id", "method", "amount_fcfa", "other_name", "external_reference", "received_fcfa", "returned_fcfa", "possible_duplicate_reference"]
        read_only_fields = fields


class CustomerCreditSerializer(serializers.ModelSerializer):
    sale_id = serializers.UUIDField(read_only=True, allow_null=True)

    class Meta:
        model = CustomerCredit
        fields = ["id", "sale_id", "amount_fcfa", "remaining_fcfa", "kind", "occurred_at"]
        read_only_fields = fields


class SaleCancellationStateSerializer(serializers.ModelSerializer):
    class Meta:
        model = SaleCancellation
        fields = ["id", "occurred_at", "created_at"]
        read_only_fields = fields


class SaleSerializer(serializers.ModelSerializer):
    state = serializers.SerializerMethodField()
    cancellation = SaleCancellationStateSerializer(read_only=True, allow_null=True)

    def get_state(self, obj) -> str:
        return "cancelled" if getattr(obj, "cancellation", None) is not None else "active"

    shop_id = serializers.UUIDField(read_only=True)
    actor_id = serializers.SerializerMethodField()
    installation_id = serializers.SerializerMethodField()
    operation_id = serializers.UUIDField(read_only=True)
    customer_id = serializers.UUIDField(read_only=True, allow_null=True)
    debt = CustomerDebtSerializer(read_only=True, allow_null=True)
    customer_credit = CustomerCreditSerializer(read_only=True, allow_null=True)
    lines = SaleLineSerializer(many=True, read_only=True)
    payments = SalePaymentSerializer(many=True, read_only=True)

    @extend_schema_field(serializers.UUIDField())
    def get_actor_id(self,obj):return public_identity(obj,self.context,"actor_id")

    @extend_schema_field(serializers.UUIDField())
    def get_installation_id(self,obj):return public_identity(obj,self.context,"installation_id")

    class Meta:
        model = Sale
        fields = ["id", "shop_id", "actor_id", "installation_id", "operation_id", "occurred_at", "created_at",
                  "reference_total_fcfa", "discount_total_fcfa", "net_total_fcfa", "stock_requires_regularization",
                  "lines", "payments", "mode", "customer_id", "debt", "customer_credit", "state", "cancellation"]
        read_only_fields = fields
