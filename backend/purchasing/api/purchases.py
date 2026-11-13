"""Atomic, manager-only purchase obligations with immutable invoice snapshots."""
import uuid
from common.services.backdating import require_allowed_backdate
from django.db import transaction
from django.db.models import F,Q
from purchasing.services.supplier_ledger import append_supplier_ledger
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_field, extend_schema_view
from rest_framework import mixins, serializers, viewsets
from rest_framework.response import Response

from common.api.fields import ExactIntegerField, StrictSerializer
from common.services.operations import json_value, original_receipt
from common.services.errors import OperationConflict
from common.models import Activity
from common.services.origins import with_public_origin,OriginReferenceConflict
from common.api.errors import OriginReferenceError
from catalogue.models import Product, ProductPack
from purchasing.models import PurchaseDocument, PurchaseLine, PurchaseReceipt, Supplier, SupplierDebt, SupplierLedgerEntry, PurchaseLineClosure
from sales.services.pricing import MAX_FCFA
from purchasing.api.suppliers import supplier_shop
from purchasing.api.purchase_receipt_serializers import ReceiptLineSerializer, ReceiptSerializer
from purchasing.services.receipts import _persist_purchase_receipt


class EventSerializer(StrictSerializer):
    id = serializers.UUIDField()
    operation_id = serializers.UUIDField()
    occurred_at = serializers.DateTimeField()
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    backdate_reason = serializers.CharField(max_length=2000, allow_blank=False, required=False)  # G15

    def validate_occurred_at(self, value):
        if value > timezone.now():
            raise serializers.ValidationError("Date future interdite.")
        return value


class PurchaseLineInput(StrictSerializer):
    id = serializers.UUIDField()
    product_id = serializers.UUIDField()
    pack_id = serializers.UUIDField(default=None, allow_null=True)
    quantity_milli = ExactIntegerField(min_value=1, max_value=MAX_FCFA)
    unit_price_fcfa = ExactIntegerField(min_value=0, max_value=MAX_FCFA)


class PurchaseInput(EventSerializer):
    external_reference = serializers.CharField(max_length=150, default="", allow_blank=True)
    order_id = serializers.UUIDField(default=None, allow_null=True)
    lines = PurchaseLineInput(many=True, allow_empty=False, max_length=500)

    def validate_lines(self, value):
        if len({line["id"] for line in value}) != len(value):
            raise serializers.ValidationError("Identifiant de ligne répété.")
        return value


class OpeningDebtInput(EventSerializer):
    amount_fcfa = ExactIntegerField(min_value=1, max_value=MAX_FCFA)


class SupplierDebtSerializer(serializers.ModelSerializer):
    document_id = serializers.UUIDField(read_only=True, allow_null=True)

    class Meta:
        model = SupplierDebt
        fields = ["id", "document_id", "amount_fcfa", "remaining_fcfa", "occurred_at", "created_at"]
        read_only_fields = fields


def closed_quantity(line):
    """G17: quantity explicitly closed as never to be received."""
    return PurchaseLineClosure.objects.filter(line_id=line.id).values_list("quantity_milli", flat=True).first() or 0


class PurchaseLineSerializer(serializers.ModelSerializer):
    received_quantity_milli = serializers.SerializerMethodField()
    awaiting_quantity_milli = serializers.SerializerMethodField()

    @extend_schema_field(serializers.IntegerField())
    def get_received_quantity_milli(self, obj):
        return sum(line.quantity_milli for line in obj.receipt_lines.all())

    @extend_schema_field(serializers.IntegerField())
    def get_awaiting_quantity_milli(self, obj):
        return obj.quantity_milli - self.get_received_quantity_milli(obj) - closed_quantity(obj)

    class Meta:
        model = PurchaseLine
        fields = ["id", "product_id", "pack_id", "product_name", "unit_label", "quantity_milli", "factor_milli",
                  "base_quantity_milli", "unit_price_fcfa", "total_fcfa", "received_quantity_milli", "awaiting_quantity_milli"]
        read_only_fields = fields


class PurchaseSerializer(serializers.ModelSerializer):
    lines = PurchaseLineSerializer(many=True, read_only=True)
    debt = SupplierDebtSerializer(read_only=True, allow_null=True)

    class Meta:
        model = PurchaseDocument
        fields = ["id", "kind", "order_id", "occurred_at", "created_at", "external_reference", "total_fcfa", "lines", "debt"]
        read_only_fields = fields


def context(shop_id, supplier_id, request):
    shop = supplier_shop(shop_id, request)
    return shop, get_object_or_404(Supplier, id=supplier_id, shop=shop)


@transaction.atomic
def create_document(shop_id, supplier_id, kind, request, data):
    shop, supplier = context(shop_id, supplier_id, request)
    canonical = json_value({**data, "supplier_id": supplier.id, "installation_id": request.device_session.installation.id})
    action = "purchase_" + kind + "_created"
    original = original_receipt(data["operation_id"], shop, request.user, action, canonical)
    if original is not None:
        return original, False
    if (PurchaseDocument.objects.filter(id=data["id"]).exists()
            or kind=="invoice" and SupplierLedgerEntry.objects.filter(id=uuid.uuid5(data['id'],'supplier-ledger:invoice')).exists()):
        raise OperationConflict()
    order = None
    if data["order_id"]:
        if kind != "invoice":
            raise serializers.ValidationError({"order_id": "Réservé aux factures."})
        order = get_object_or_404(PurchaseDocument, id=data["order_id"], shop=shop, supplier=supplier, kind="order",operation__shop=shop)
    snapshots = []
    total = 0
    for position, line in enumerate(data["lines"]):
        if PurchaseLine.objects.filter(id=line["id"]).exists():
            raise OperationConflict()
        product = get_object_or_404(Product, id=line["product_id"], shop=shop, is_active=True)
        pack = get_object_or_404(ProductPack, id=line["pack_id"], product=product, is_active=True) if line["pack_id"] else None
        factor = pack.base_quantity_milli if pack else 1000
        scaled = line["quantity_milli"] * factor
        if scaled % 1000 or (product.unit_kind == "piece" and scaled % 1000000):
            raise serializers.ValidationError({"lines": "Quantité incompatible avec l'unité de base."})
        base = scaled // 1000
        amount = (line["quantity_milli"] * line["unit_price_fcfa"] + 500) // 1000
        total += amount
        if base > MAX_FCFA or total > MAX_FCFA:
            raise serializers.ValidationError({"lines": "Montant ou quantité hors limites."})
        snapshots.append(dict(id=line["id"], product=product, pack=pack, position=position,
                              product_name=product.name, unit_label=pack.name if pack else product.unit_label,
                              quantity_milli=line["quantity_milli"], factor_milli=factor, base_quantity_milli=base,
                              unit_price_fcfa=line["unit_price_fcfa"], total_fcfa=amount))
    return _persist_purchase_document(shop=shop,supplier=supplier,actor=request.user,installation=request.device_session.installation,
        kind=kind,data=data,canonical=canonical,snapshots=snapshots,order=order)


def _persist_purchase_document(*,shop,supplier,actor,installation,kind,data,canonical,snapshots,order=None,
        origin_capture=None,source_snapshot=None,recovery_review=None):
    """Append only the obligation and its frozen lines inside the caller's transaction."""
    total=sum(row['total_fcfa'] for row in snapshots)
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor, action="purchase_"+kind+"_created",
                                    target_id=data["id"], reason=data["reason"], details={"request": canonical})
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=event,capture=origin_capture)
        event.details['recovery']=json_value({'capture_id':origin_capture.id,'origin_user_id':origin_capture.origin_user_id,
            'origin_installation_id':origin_capture.origin_installation_id,'origin_recorded_at':origin_capture.origin_recorded_at,
            'origin_installation_verified':False,'source_snapshot':source_snapshot,'review':recovery_review})
    document = PurchaseDocument.objects.create(id=data["id"], shop=shop, supplier=supplier, kind=kind, order=order,
                                               operation=event, installation=installation,
                                               occurred_at=data["occurred_at"], external_reference=data["external_reference"], total_fcfa=total)
    PurchaseLine.objects.bulk_create([PurchaseLine(document=document, **line) for line in snapshots])
    if kind == "invoice":
        identity={'id':source_snapshot['debt']['id']} if origin_capture is not None else {}
        SupplierDebt.objects.create(shop=shop, supplier=supplier, document=document, operation=event,
                                    installation=installation, occurred_at=data["occurred_at"],
                                    amount_fcfa=total, remaining_fcfa=total,**identity)
    receipt = json_value(PurchaseSerializer(document).data)
    event.details["response"] = receipt
    event.save(update_fields=["details", "updated_at"])
    if kind == "invoice":
        append_supplier_ledger(shop, supplier, event, document.id, "invoice", document.occurred_at,
                               debt_delta=total, external_reference=document.external_reference)
    return receipt, True


@transaction.atomic
def open_supplier_debt(shop_id, supplier_id, request, data):
    shop, supplier = context(shop_id, supplier_id, request)
    canonical = json_value({**data, "supplier_id": supplier.id, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "supplier_debt_opened", canonical)
    if original is not None:
        return original, False
    if SupplierDebt.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    return _persist_supplier_debt(shop=shop,supplier=supplier,actor=request.user,installation=request.device_session.installation,data=data,canonical=canonical)


def _persist_supplier_debt(*,shop,supplier,actor,installation,data,canonical,origin_capture=None,source_snapshot=None,recovery_review=None):
    """Append the opening payable and its ledger, without an invoice or payment."""
    event = Activity.objects.create(id=data["operation_id"], shop=shop, actor=actor, action="supplier_debt_opened",
                                    target_id=data["id"], reason=data["reason"], details={"request": canonical})
    if origin_capture is not None:
        from backups.models import RecoveredOperationOrigin
        RecoveredOperationOrigin.objects.create(operation=event,capture=origin_capture)
        event.details['recovery']=json_value({'capture_id':origin_capture.id,'origin_user_id':origin_capture.origin_user_id,
            'origin_installation_id':origin_capture.origin_installation_id,'origin_recorded_at':origin_capture.origin_recorded_at,
            'origin_installation_verified':False,'source_snapshot':source_snapshot,'review':recovery_review})
    debt = SupplierDebt.objects.create(id=data["id"], shop=shop, supplier=supplier, operation=event,
                                       installation=installation, occurred_at=data["occurred_at"],
                                       amount_fcfa=data["amount_fcfa"], remaining_fcfa=data["amount_fcfa"])
    receipt = json_value(SupplierDebtSerializer(debt).data)
    event.details["response"] = receipt
    event.save(update_fields=["details", "updated_at"])
    append_supplier_ledger(shop, supplier, event, debt.id, "opening_debt", debt.occurred_at,
                           debt_delta=debt.amount_fcfa)
    return receipt, True


@extend_schema_view(list=extend_schema(responses={200:PurchaseSerializer(many=True),409:OriginReferenceError}),
    retrieve=extend_schema(responses={200:PurchaseSerializer,409:OriginReferenceError}))
class PurchaseViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = PurchaseDocument.objects.none()
    serializer_class = PurchaseSerializer
    kind = "invoice"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        shop, supplier = context(self.kwargs["shop_id"], self.kwargs["supplier_id"], self.request)
        rows=with_public_origin(PurchaseDocument.objects.filter(shop=shop,supplier=supplier,kind=self.kind),actor_path='operation__actor_id')
        # Relations in an old/imported database are not authority to another shop.
        # Check only identities here, before loading debt amounts or frozen lines.
        if (rows.filter(kind='invoice',debt__isnull=True).exists()
                or rows.filter(kind='order',debt__isnull=False).exists()
                or rows.filter(debt__isnull=False).exclude(debt__shop=shop,debt__supplier=supplier,
                    debt__operation_id=F('operation_id')).exists()
                or rows.filter(order__isnull=False).exclude(order__shop=shop,order__supplier=supplier,
                    order__kind='order',order__operation__shop=shop).exists()):
            raise OriginReferenceConflict()
        lines=PurchaseLine.objects.filter(document_id__in=rows.values('id'))
        if (lines.exclude(product__shop=shop).exists()
                or lines.exclude(Q(pack__isnull=True)|Q(pack__product_id=F('product_id'))).exists()):
            raise OriginReferenceConflict()
        return rows.select_related("debt").prefetch_related("lines__receipt_lines").order_by("created_at", "id")

    @extend_schema(request=PurchaseInput, responses={200: PurchaseSerializer, 201: PurchaseSerializer})
    def create(self, request, *args, **kwargs):
        context(self.kwargs["shop_id"], self.kwargs["supplier_id"], request)
        serializer = PurchaseInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = create_document(self.kwargs["shop_id"], self.kwargs["supplier_id"], self.kind, request, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)


class SupplierDebtViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = SupplierDebt.objects.none()
    serializer_class = SupplierDebtSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        shop, supplier = context(self.kwargs["shop_id"], self.kwargs["supplier_id"], self.request)
        return SupplierDebt.objects.filter(shop=shop, supplier=supplier).order_by("occurred_at", "id")

    @extend_schema(request=OpeningDebtInput, responses={200: SupplierDebtSerializer, 201: SupplierDebtSerializer})
    def create(self, request, *args, **kwargs):
        context(self.kwargs["shop_id"], self.kwargs["supplier_id"], request)
        serializer = OpeningDebtInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, created = open_supplier_debt(self.kwargs["shop_id"], self.kwargs["supplier_id"], request, serializer.validated_data)
        return Response(receipt, status=201 if created else 200)


class ReceiptLineInput(StrictSerializer):
    line_id = serializers.UUIDField()
    quantity_milli = ExactIntegerField(min_value=1, max_value=MAX_FCFA)


class ReceiptInput(EventSerializer):
    lines = ReceiptLineInput(many=True, allow_empty=False, max_length=500)

    def validate_lines(self, value):
        if len({line["line_id"] for line in value}) != len(value):
            raise serializers.ValidationError("Ligne de facture répétée.")
        return value


def receipt_document(shop, supplier, document_id):
    """Reject inconsistent historical links before reading receipt cost evidence."""
    document = get_object_or_404(PurchaseDocument, id=document_id, shop=shop,
        supplier=supplier, supplier__shop=shop, operation__shop=shop, kind="invoice")
    if document.lines.exclude(product__shop=shop).exists() or document.lines.filter(
            pack__isnull=False).exclude(pack__product_id=F("product_id")).exists():
        raise OperationConflict()
    return document


@transaction.atomic
def receive_purchase(shop_id, supplier_id, document_id, request, data):
    shop, supplier = context(shop_id, supplier_id, request)
    document = receipt_document(shop, supplier, document_id)
    canonical = json_value({**data, "document_id": document.id, "installation_id": request.device_session.installation.id})
    original = original_receipt(data["operation_id"], shop, request.user, "purchase_received", canonical)
    if original is not None:
        return original, False
    require_allowed_backdate(shop, request.user, data["occurred_at"], data.get("backdate_reason"))
    if PurchaseReceipt.objects.filter(id=data["id"]).exists():
        raise OperationConflict()
    prepared = []
    for item in data["lines"]:
        line = get_object_or_404(PurchaseLine, id=item["line_id"], document=document)
        received = sum(part.quantity_milli for part in line.receipt_lines.all())
        quantity = item["quantity_milli"]
        if received + closed_quantity(line) + quantity > line.quantity_milli:  # G17: closed remainder
            raise serializers.ValidationError({"lines": "La réception dépasse le reliquat de facture."})
        scaled = quantity * line.factor_milli
        if scaled % 1000 or (line.product.unit_kind == "piece" and scaled % 1000000):
            raise serializers.ValidationError({"lines": "Quantité incompatible avec l'unité de base."})
        prepared.append((line, quantity, scaled // 1000))
    return _persist_purchase_receipt(shop=shop, document=document, actor=request.user,
        installation=request.device_session.installation, data=data, canonical=canonical,
        prepared=prepared)


class PurchaseReceiptViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    queryset = PurchaseReceipt.objects.none()
    serializer_class = ReceiptSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.queryset
        shop, supplier = context(self.kwargs["shop_id"], self.kwargs["supplier_id"], self.request)
        document = receipt_document(shop, supplier, self.kwargs["document_id"])
        return PurchaseReceipt.objects.filter(document=document).select_related("operation").prefetch_related(
            "lines__line", "lines__receipt__operation").order_by("created_at", "id")

    @extend_schema(request=ReceiptInput, responses={200: ReceiptSerializer, 201: ReceiptSerializer})
    def create(self, request, *args, **kwargs):
        context(self.kwargs["shop_id"], self.kwargs["supplier_id"], request)
        serializer = ReceiptInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        result, created = receive_purchase(self.kwargs["shop_id"], self.kwargs["supplier_id"], self.kwargs["document_id"], request, serializer.validated_data)
        return Response(result, status=201 if created else 200)
