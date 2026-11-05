"""Explicit responsible review before replaying frozen source operations."""
from drf_spectacular.utils import extend_schema,extend_schema_serializer,extend_schema_field,OpenApiResponse,PolymorphicProxySerializer
from rest_framework import serializers,viewsets
from rest_framework.response import Response

from backups.api.recovery_devices import RecoveryDeviceAccessError
from backups.api.recovery_journals import RecoveryJournalParser
from backups.services.recovery_integration import RecoveryIntegrationInput,integration_context,preview_recovery_integration,integrate_recovery_operation
from backups.services.recovery_sale_snapshot import FrozenSaleSnapshot
from sales.api.sale_serializers import SaleSerializer
from backups.services.recovery_collection_snapshot import FrozenCollectionSnapshot
from customers.api.payment_serializers import CustomerPaymentSerializer
from backups.services.recovery_customer_snapshots import FrozenCustomerCreationSnapshot,FrozenOpeningDebtSnapshot
from customers.api.client_serializers import ClientSerializer
from customers.api.debts import CustomerDebtSerializer
from backups.services.recovery_customer_revisions import FrozenCustomerUpdateSnapshot,FrozenDueChangeSnapshot
from backups.services.recovery_customer_refunds import FrozenCustomerRefund
from backups.services.recovery_customer_allocations import FrozenCustomerAllocation
from backups.services.recovery_return_settlements import FrozenReturnSettlement
from backups.services.recovery_sale_returns import FrozenSaleReturn
from backups.services.recovery_return_receipts import FrozenReturnReceipt
from stock.api.return_receipts import ReturnReceiptSerializer
from backups.services.recovery_unsellable_decisions import FrozenUnsellableDecision
from stock.api.unsellable_decisions import NonSellableDecisionSerializer
from backups.services.recovery_sale_cancellations import FrozenSaleCancellation
from sales.api.sale_cancellations import SaleCancellationSerializer
from customers.api.return_credit_settlements import ReturnCreditSettlementSerializer
from sales.api.sale_returns import SaleReturnSerializer
from customers.api.regularizations import PaymentRegularizationSerializer
from backups.services.recovery_supplier_snapshots import FrozenSupplierCreation,FrozenSupplierOpeningDebt,FrozenSupplierOpeningBalance
from purchasing.api.suppliers import SupplierSerializer
from purchasing.api.purchases import SupplierDebtSerializer
from purchasing.api.supplier_credits import CreditSerializer as SupplierCreditSerializer
from backups.services.recovery_supplier_payment_snapshots import FrozenSupplierPayment,FrozenSupplierPaymentAllocation
from purchasing.api.supplier_payments import PaymentSerializer as SupplierPaymentSerializer,AllocationReceiptSerializer as SupplierPaymentAllocationReceipt
from backups.services.recovery_purchase_snapshots import FrozenPurchaseDocument
from purchasing.api.purchases import PurchaseSerializer
from backups.services.recovery_purchase_receipts import FrozenPurchaseReceipt
from purchasing.api.purchase_receipt_serializers import ReceiptSerializer as PurchaseReceiptSerializer
from backups.services.recovery_transfer_receipts import FrozenTransferReceipt
from stock.api.transfers import TransferReceiptSerializer
from backups.services.recovery_catalogue_snapshots import ProductRevisionSnapshot,PackRevisionSnapshot
from catalogue.api.catalogue_serializers import CostProductSerializer,PackSerializer


class RecoveryIntegrationIssue(serializers.Serializer):
    code=serializers.CharField()
    ids=serializers.ListField(child=serializers.UUIDField())


class RecoveryStockImpact(serializers.Serializer):
    product_id=serializers.UUIDField()
    reservation_id=serializers.UUIDField(allow_null=True,help_text="Use this current ID to preview an explicit reconciliation after refusal.")
    validated_before_milli=serializers.CharField()
    reserved_before_milli=serializers.CharField()
    available_before_milli=serializers.CharField()
    validated_after_milli=serializers.CharField()
    reserved_after_milli=serializers.CharField()
    available_after_milli=serializers.CharField()


@extend_schema_serializer(many=False)
class RecoveryAllocationImpact(serializers.Serializer):
    allocation_id=serializers.UUIDField()
    debt_id=serializers.UUIDField()
    requested_amount_fcfa=serializers.IntegerField(min_value=1)
    original_applied_fcfa=serializers.IntegerField(min_value=0)
    remaining_before_fcfa=serializers.IntegerField(min_value=0)
    applied_fcfa=serializers.IntegerField(min_value=0)
    remaining_after_fcfa=serializers.IntegerField(min_value=0)


class RecoveryDebtAllocationImpact(serializers.Serializer):
    debt_id=serializers.UUIDField()
    requested_fcfa=serializers.IntegerField(min_value=1)
    remaining_before_fcfa=serializers.IntegerField(min_value=0,allow_null=True)
    remaining_after_fcfa=serializers.IntegerField(min_value=0,allow_null=True)
    can_apply=serializers.BooleanField(help_text="Whether this target accepts its exact total, or the operation is already known. Overall preview can_apply still controls integration.")


@extend_schema_serializer(many=False)
class RecoveryIntegrationPreview(serializers.Serializer):
    capture_id=serializers.UUIDField()
    operation_type=serializers.ChoiceField(choices=["sale","customer_collection","customer_creation","customer_opening_debt","customer_update","customer_due_change","customer_refund","customer_allocation","return_credit_settlement","sale_return","return_receipt","non_sellable_decision","sale_cancellation","supplier_creation","supplier_opening_debt","supplier_opening_balance","supplier_payment","supplier_payment_allocation","purchase_order","purchase_invoice","purchase_receipt","transfer_receipt","product_update","pack_update"])
    version=serializers.IntegerField(min_value=0)
    preview_token=serializers.CharField()
    can_apply=serializers.BooleanField()
    hold_id=serializers.UUIDField(allow_null=True)
    collection_review_id=serializers.UUIDField(allow_null=True,required=False)
    can_mark_pending=serializers.BooleanField(required=False)
    review_state=serializers.ChoiceField(choices=["pending","refused","validated"],allow_null=True)
    review_version=serializers.IntegerField(min_value=0)
    can_hold=serializers.BooleanField()
    can_refuse=serializers.BooleanField()
    outcome=serializers.ChoiceField(choices=["new","already_known","blocked"])
    snapshot=serializers.SerializerMethodField()
    current_state=serializers.JSONField(allow_null=True,required=False)
    stocks=RecoveryStockImpact(many=True)
    allocation_impacts=RecoveryAllocationImpact(many=True,required=False)
    debt_allocation_impacts=RecoveryDebtAllocationImpact(many=True,required=False)
    cash_delta_by_method_fcfa=serializers.DictField(child=serializers.IntegerField())
    new_debt_fcfa=serializers.IntegerField(min_value=0)
    new_customer_credit_fcfa=serializers.IntegerField(min_value=0)
    new_supplier_debt_fcfa=serializers.IntegerField(min_value=0,required=False)
    new_supplier_advance_fcfa=serializers.IntegerField(min_value=0,required=False)
    new_supplier_credit_fcfa=serializers.IntegerField(min_value=0,required=False)
    supplier_debt_delta_fcfa=serializers.IntegerField(required=False)
    supplier_advance_delta_fcfa=serializers.IntegerField(required=False)
    supplier_credit_delta_fcfa=serializers.IntegerField(required=False)
    origin_user_id=serializers.UUIDField()
    origin_installation_id=serializers.UUIDField()
    origin_recorded_at=serializers.DateTimeField()
    issues=RecoveryIntegrationIssue(many=True)
    notices=RecoveryIntegrationIssue(many=True,required=False)
    has_possible_duplicate=serializers.BooleanField(required=False)
    origin_installation_verified=serializers.BooleanField(required=False,help_text="Whether retained authenticated operation evidence records this source device; opaque journal UUIDs alone are declarations. Missing old native client or catalogue evidence stays unverified.")

    @extend_schema_field(PolymorphicProxySerializer(component_name="RecoverySourceSnapshot",
        serializers=[FrozenSaleSnapshot,FrozenCollectionSnapshot,FrozenCustomerCreationSnapshot,FrozenOpeningDebtSnapshot,FrozenCustomerUpdateSnapshot,FrozenDueChangeSnapshot,FrozenCustomerRefund,FrozenCustomerAllocation,FrozenReturnSettlement,FrozenSaleReturn,FrozenReturnReceipt,FrozenUnsellableDecision,FrozenSaleCancellation,FrozenSupplierCreation,FrozenSupplierOpeningDebt,FrozenSupplierOpeningBalance,FrozenSupplierPayment,FrozenSupplierPaymentAllocation,FrozenPurchaseDocument,FrozenPurchaseReceipt,FrozenTransferReceipt,ProductRevisionSnapshot,PackRevisionSnapshot],resource_type_field_name=None))
    def get_snapshot(self,obj):return obj["snapshot"]


@extend_schema_serializer(many=False)
class RecoveryIntegrationReceipt(serializers.Serializer):
    id=serializers.UUIDField()
    operation_id=serializers.UUIDField()
    capture_id=serializers.UUIDField()
    version=serializers.IntegerField(min_value=1)
    business_operation_id=serializers.UUIDField()
    outcome=serializers.ChoiceField(choices=["applied","already_known"])
    reviewed_by=serializers.UUIDField()
    reviewed_installation_id=serializers.UUIDField()
    reviewed_at=serializers.DateTimeField()
    reason=serializers.CharField()
    related_integration_decision_ids=serializers.ListField(child=serializers.UUIDField(),required=False,
        help_text='Cancellation acceptance includes its linked child review in the same transaction; retries keep these IDs.')
    business_receipt=serializers.SerializerMethodField(help_text="First persisted acknowledgement of this operation; read the entity for its current state and balances.")

    @extend_schema_field(PolymorphicProxySerializer(component_name="RecoveryBusinessReceipt",
        serializers=[SaleSerializer,CustomerPaymentSerializer,ClientSerializer,CustomerDebtSerializer,PaymentRegularizationSerializer,ReturnCreditSettlementSerializer,SaleReturnSerializer,ReturnReceiptSerializer,NonSellableDecisionSerializer,SaleCancellationSerializer,SupplierSerializer,SupplierDebtSerializer,SupplierCreditSerializer,SupplierPaymentSerializer,SupplierPaymentAllocationReceipt,PurchaseSerializer,PurchaseReceiptSerializer,TransferReceiptSerializer,CostProductSerializer,PackSerializer],resource_type_field_name=None))
    def get_business_receipt(self,obj):return obj["business_receipt"]


ERRORS={401:OpenApiResponse(RecoveryDeviceAccessError,description="Missing, expired or revoked current authentication."),
    403:OpenApiResponse(RecoveryDeviceAccessError,description="Current responsible role required; historical UUIDs grant no rights."),
    404:OpenApiResponse(RecoveryDeviceAccessError,description="Inaccessible shop, foreign capture or historical recovery generation."),
    400:OpenApiResponse({"type":"object","additionalProperties":True},description="Invalid frozen source arithmetic, unsupported source type or invalid input; the raw copy remains captured."),
    409:OpenApiResponse(RecoveryDeviceAccessError,description="Changed source digest, preview, version, command or existing operation; no partial effect."),
    415:OpenApiResponse(RecoveryDeviceAccessError,description="application/json required.")}


class RecoveryIntegrationViewSet(viewsets.GenericViewSet):
    queryset=None
    serializer_class=RecoveryIntegrationPreview
    parser_classes=[RecoveryJournalParser]

    def authorize(self):
        return integration_context(self.kwargs["shop_id"],self.request.user,self.request.device_session.id)

    @extend_schema(responses={200:RecoveryIntegrationPreview,**ERRORS},description=(
        "Current responsible previews one current-generation capture before replay. Implemented adapters: sale, customer_collection, "
        "customer_creation, customer_opening_debt, customer_update, customer_due_change, customer_refund, customer_allocation, "
        "return_credit_settlement, sale_return, return_receipt, non_sellable_decision, sale_cancellation, supplier_creation, "
        "supplier_opening_debt, supplier_opening_balance, supplier_payment, supplier_payment_allocation, purchase_order, purchase_invoice, "
        "purchase_receipt, transfer_receipt, product_update and pack_update. "
        "A purchase_receipt preserves its invoice line IDs, receipt row IDs, stock movement IDs, received quantities and frozen cost evidence; "
        "missing historical proof or charge dependencies blocks replay without substituting current costs. "
        "A transfer_receipt carries schema_version 1, the complete native original_request including transfer_id and installation_id, "
        "and its complete frozen native receipt. The archived departure must survive and belong to this destination shop. Each partial "
        "receipt retains its explicitly chosen existing destination product, unit, received-before quantity, movement UUID and exact "
        "allocated departure cost. No catalogue matching or conversion is inferred. Missing departure/product, inconsistent frozen "
        "costs or changed cumulative receipt context blocks application and requires a new explicit review. "
        "Catalogue revisions carry strict before and after facts with versions; conflicting current revisions remain captured and blocked "
        "without overwriting prices, selecting a winner or changing stock. "
        "A customer_creation carries original_request and customer initial "
        "ID/name/phone/version 1/aware creation timestamps; equal contact information only produces a duplicate hint, never a merge. "
        "A customer_opening_debt carries original_request and opening_debt with full initial balance, no sale, UTC effective/deadline "
        "and due_version 1. Its customer/dependencies must exist first; it creates no cash or turnover. "
        "A customer_update supplies original_request and customer_update with strict before/after contact facts: "
        "the current base version and all immutable before fields must match, the version increases by one and only declared fields change. "
        "A customer_due_change supplies original_request and due_change with ID/customer, before/after UTC deadlines and versions, "
        "and occurred_at; it contains no amount or balance. Both need existing targets/dependencies; conflicts stay captured "
        "and block application without automatic priority or merge. current_state exposes only the authorized target. "
        "A customer_refund supplies original_request and refund with ID/customer, exactly one payment_id or credit_id, "
        "original before/after unallocated credit, exact amount/method/reference and occurred_at. It requires its source "
        "and sufficient current credit. A changed balance requires a new preview; the refund amount is never capped. "
        "Missing/insufficient credit keeps the raw refund blocked with its full proposed cash impact, pending explicit resolution. "
        "A customer_allocation supplies original_request and allocation with ID/customer, exactly one payment_id or credit_id, "
        "before/after unallocated credit, exact total, occurred_at and the ordered allocation ID/debt_id/amount list. "
        "Source and target debts must exist in this shop for the same customer. Repeated debts are aggregated to check capacity "
        "without merging their distinct row UUIDs. Insufficient credit/debt or missing dependencies blocks the whole operation; "
        "no partial application, new debt selection or cash effect. debt_allocation_impacts shows current target capacities, "
        "with null after when unavailable; global can_apply controls integration. Changed balances require a new preview. "
        "A return_credit_settlement supplies original_request and settlement with ID, sale/return/customer (nullable), kind, "
        "original credit before/after, amount, occurred_at and original created_at, method/reference and allocation list. "
        "Its existing return claim must belong to this shop and sale/customer. An anonymous claim permits refund only; "
        "allocations require identified same-customer debts and retain the native ban on repeated target debts. "
        "Missing references, insufficient claim/debt and changed context block the whole operation without stock or cash effects. "
        "A sale_return supplies original_request and sale_return with ID/sale/customer, effective and source creation UTC dates, "
        "amount, frozen debt reduction/credit, and line IDs, original sale-line IDs, base quantities, billed net totals and unit kinds. "
        "Arithmetic is checked against frozen billed values, never today's catalogue. Current cumulative returned quantities "
        "must allow the exact source line values under cumulative half-up rounding. Current original-sale debt must produce "
        "the exact source debt/credit split; a changed financial or quantity/value context remains blocked for explicit resolution. "
        "A sale already cancelled blocks a new return; matching known returns remain acknowledgements only. "
        "Dependencies recovered later change the preview token and require renewed review. Already committed creations are "
        "compared against initial receipts, without resetting later names, deadlines or balances. Old native client evidence "
        "missing a device remains explicitly unverified in origin_installation_verified/notices; it never rebinds the original device. "
        "A collection carries original_request and collection with initial requested/applied allocations, parts and unallocated total. "
        "Each recovered allocation is capped by its original applied amount and today's remaining debt, in original order. "
        "The preview shows allocation_impacts, real net cash by method and any unallocated excess; it never auto-settles other debts. "
        "For a collection, collection_review_id/review_state/review_version and can_mark_pending/can_refuse describe "
        "the financial review. Use the collection-review endpoint; can_hold remains false because no stock is reserved. "
        "A return_receipt carries original_request and receipt with stable receipt/sale/return/line/product IDs, original base unit, "
        "physical quantity/condition, effective and source creation times, deterministic sellable movement ID, integer legacy cost projection "
        "and its frozen initial exact cost (version 1, no revision, or null for legacy receipts). Exact large cost integers are decimal strings. "
        "Only sellable goods add available stock; unsellable goods remain in a separate pool. Cumulative physical quantities and exact "
        "cost shares must match the original sale and retained cost source in this shop. A missing referenced sale cost, incompatible "
        "cost share, missing parent/dependency or later recovery inventory adjustment keeps the raw copy blocked for explicit resolution. "
        "Known receipt creation facts remain distinct from later physical dispositions or cost revisions. "
        "A non_sellable_decision carries original_request and decision with the owned physical receipt/sale/return/line/product IDs, "
        "kind restock/supplier_return/scrap, original quantity and remaining pool, unit, explicit sellable confirmation, optional supplier, "
        "deterministic restock movement, timestamps and its historical exact cost version (or explicit legacy absence). Source cost versions "
        "are resolved only through this owned receipt. Current remaining quantity may differ when still sufficient and the exact original "
        "cost share remains compatible; the original after quantity stays in source facts and the acknowledgement records current remaining. "
        "Insufficient pool, missing supplier/cost/dependency, or incompatible cumulative rounding blocks the entire raw operation. "
        "Only confirmed restock adds available stock; supplier departure and scrap only consume this non-sellable pool, without new loss or money. "
        "A sale_cancellation carries original_request and cancellation with ID/sale, optional sale_return_id, timestamps and nullable "
        "compensating_return using the complete sale_return payload. Parent/child IDs, generated line IDs, actor/device/time/reason and "
        "frozen remainder must agree. A new compensating return also needs its own identical current-generation captured sale_return "
        "copy for distinct lineage; the parent may depend on this child internally, but the child must not depend on the parent. "
        "The parent preview binds the child copy, preview, versions and current quantities/debt. Missing copies, mismatches, another "
        "cancellation, later commercial returns or incompatible child finance block the whole cancellation. If the sale was already "
        "fully returned, no compensating return is created. A matching already committed child is never applied again. "
        "Supplier payload schema_version 1 has original_request and supplier, opening_debt or opening_balance for supplier_creation, "
        "supplier_opening_debt and supplier_opening_balance. Creation freezes id/name/phone/version 1; its journal time is a declared "
        "source timestamp, never invented as a proven old server creation time. No equal-contact merge is inferred. Openings freeze "
        "supplier ID, amount equal to initial remaining amount, effective and source creation times, with no invoice for debt and "
        "no return/reference/applied amount for opening advance or credit. The kind stays explicit. Missing suppliers/dependencies, "
        "future dates, conflicting UUIDs or source identity block replay; new_supplier_* impacts stay distinct from customer receivables. "
        "A supplier_payment payload carries original_request and payment: initial receipt IDs, supplier/device, UTC times, exact ordered "
        "parts and initial unallocated total equal to the full amount; allocations must be empty. Every actual declared part survives "
        "even if its reference is possibly reused. A supplier_payment_allocation payload carries original_request and allocation: "
        "operation/payment/supplier IDs, original before/after advance, recorded_at and stable allocation row IDs in original target order. "
        "Its journal occurred_at equals the original receipt's recorded_at. Exact amounts must all fit current advance and scoped debts; "
        "no partial cap, target substitution or automatic cash effect. The preview binds current balances and shows supplier debt/advance "
        "deltas separately. Missing/foreign parents, funds, row UUID collisions or incompatible debts block the whole allocation. "
        "A purchase_order or purchase_invoice payload carries original_request and purchase: the initial native receipt plus supplier_id, "
        "with kind, optional order, source UTC creation/effective dates, exact original line IDs, names, quantities, factors, prices and totals. "
        "Initial received quantities are zero. Only an invoice has its full initial debt and stable debt UUID. Supplier, products, "
        "packs and optional same-supplier order must exist in this shop; current catalogue values never replace billed snapshots. "
        "Missing dependencies, incompatible base quantities, conflicting UUIDs or inconsistent totals block the whole document. "
        "An order creates no debt, stock or money; an invoice creates only its explicit debt and supplier ledger, without receipt or payment. "
        "For a sale, payload schema_version 1 carries original_request and a frozen sale snapshot (IDs, unit/factor/price/discount snapshots, "
        "payments and UTC due_at). Source arithmetic and identity are checked without repricing from today's catalogue. "
        "Missing references/dependencies, changed base unit, UUID conflicts and later recovery inventory adjustments block application. "
        "No business effect is created by this read. The preview token binds the current context; opaque source UUIDs never create accounts."))
    def preview(self,request,*args,**kwargs):
        self.authorize()
        return Response(preview_recovery_integration(shop_id=self.kwargs["shop_id"],actor=request.user,
            session_id=request.device_session.id,capture_id=self.kwargs["pk"]))

    @extend_schema(request=RecoveryIntegrationInput,responses={200:RecoveryIntegrationReceipt,201:RecoveryIntegrationReceipt,**ERRORS},description=(
        "Current responsible explicitly accepts frozen source facts using the latest preview_token, previous_version and reason. "
        "Current rights, source integrity and preview are rechecked under SQLite's write reservation. "
        "A new sale uses its stable source operation/entity UUIDs, effective UTC date and frozen prices/factors. Stock, "
        "payments, unpaid debt/overpayment, cost snapshots, stock alerts, origin link and versioned decision commit together. "
        "The verified reviewer is the server writer; public actor/installation references remain historical source UUIDs, "
        "without creating or activating source accounts. "
        "A ready transfer_receipt adds only its physically received sellable quantity to the chosen destination products, preserving "
        "source receipt/row/movement UUIDs and frozen departure cost shares. The source stock is not deducted again; remaining transit "
        "is derived from received quantities. Receipt, stock, exact costs, origin and decision commit together, without cash, debt, "
        "automatic product selection or restored rights. Known receipts return their first acknowledgement after live authorization. "
        "Recovered collections preserve every real payment part; "
        "only originally applied allocations can reduce current debt. Remaining money becomes explicit unallocated credit "
        "requiring regularization, without automatic refund or allocation. Cash, debt, ledger, origin and review commit atomically. "
        "Customer/debt creations use stable source UUIDs and initial effective facts. Customer creation has no financial "
        "effect; opening debt adds only the explicit receivable and its ledger entry. Their origin and decision commit "
        "atomically; failures leave their raw copies intact. Matching known creations return their initial receipt "
        "without overwriting current contact/deadline/balance or fabricating missing legacy device evidence. "
        "Contact revisions change only declared contact fields at the exact current version. Deadline revisions change "
        "only the matching deadline/version and append a zero-value dated ledger entry, preserving current debt amounts. "
        "They retain source reason, before/after and origin with an atomic decision; conflicting revisions remain blocked. "
        "A ready customer_refund debits the chosen payment advance or sale credit by its exact original amount, "
        "creates one negative cash movement and one customer ledger entry at the source time, without reopening debt, "
        "creating a collection or restoring source accounts. Source/origin/reviewer/decision commit together. "
        "Its first acknowledgement uses current remaining credit; later copies are matched against preserved original "
        "source facts and never apply that refund again. A ready customer_allocation consumes the exact existing source credit "
        "and exact chosen debt amounts in its original row order. It creates a zero-net customer ledger entry with matching "
        "debt/credit decreases and no cash, collection, refund or stock. The rows, source, debts, origin and review decision "
        "commit together; a failure leaves all balances unchanged. Matching known allocations retain their first acknowledgement "
        "after later payments/refunds or another recovery generation. A ready return_credit_settlement consumes its "
        "existing return claim by the exact refund/allocation amount. Refund creates one real negative cash movement; "
        "allocation reduces only the chosen debts without cash. Customer ledger exists only for an identified sale. "
        "Its original created_at remains in the source snapshot; the first acknowledgement records the replay's created_at "
        "and current remaining claim. It creates no customer, return, physical receipt, stock or restored right. "
        "All effects, lineage and review commit atomically; known receipts survive later settlements and recovery generations. "
        "A ready sale_return creates the stable source return/line UUIDs, reduces only its own sale debt first, and creates "
        "the frozen remaining claim, with one identified customer ledger entry and original actor provenance. It creates no "
        "cash, refund, physical receipt or stock, and does not reprice historical units. Its source created_at is preserved "
        "in the snapshot; the acknowledgement records the replay creation time. Quantities, debt, return, lines, origin "
        "and decision commit together. Known creation receipts survive later settlements without resetting remaining credit. "
        "A ready return_receipt creates its original physical receipt and frozen cost UUID, with its retained original sale-cost reference "
        "or explicit legacy absence. Sellable quantity creates its deterministic stock movement and current stock alert observation; "
        "unsellable quantity creates no available stock. It creates no money, commercial return or customer ledger. Receipt, cost, stock, "
        "origin and decision commit together. Source creation time remains in the snapshot and replay creation time in its acknowledgement. "
        "Known receipts return their first acknowledgement after dispositions/revisions without restoring previous pool balances or costs. "
        "A ready non_sellable_decision consumes only its actual physical quantity. Restock writes its deterministic available-stock "
        "movement and observes alerts; supplier departure appends a zero-value supplier ledger trace without debt, credit, cash or automatic "
        "supplier return invoice. Scrap creates no second loss of available stock. The historical selected cost, source facts and first "
        "acknowledgement remain distinct from later current cost revisions and pool balances. Pool, stock, supplier trace, origin and "
        "decision commit atomically. Known receipts never consume the quantity again. A sale_cancellation commits the parent state, "
        "compensating return, own-sale debt decrease, remaining claim, identified ledger, distinct origins and linked parent/child "
        "reviews in one transaction. Child review command UUID is derived from this parent review; failure of either rolls back both. "
        "The response exposes related_integration_decision_ids. Source creation times stay in metadata and first acknowledgements "
        "record replay creation times. Existing child returns and known parent receipts are recognized without another effect. "
        "No physical receipt, restock, cash or automatic refund is inferred. Other operation recovery "
        "and blocked-conflict resolution remain to implement. "
        "Supplier creation adds only its stable identity. Supplier opening debt, advance and credit each append their separate "
        "opening position and exact supplier ledger entry, without cash, purchase document, goods, payment, return or automatic offset. "
        "The currently authorized reviewer writes the rows; journal identity/time and opening source creation time remain in metadata, "
        "not authentication rights. New opening acknowledgements record replay creation time. A matching known opening requires "
        "its preserved initial receipt, immutable model facts and ledger; later allocations/refunds never reset balances. "
        "Source, lineage, balances, ledger and review commit atomically. Supplier statements and opening-credit history project the "
        "historical actor only after shop scoping, refusing contradictory foreign lineage. "
        "Supplier payments create each negative cash movement once and their full unallocated advance with its supplier ledger. "
        "New cash UUIDs derive from stable part UUIDs; existing legacy cash UUIDs are retained on recognition, not rewritten. "
        "Supplier allocations reduce only the original chosen debts and advance by exact amounts, with stable source row UUIDs, "
        "one dated ledger entry and no cash or invoice. An otherwise sufficient changed advance yields the actual replay remainder, "
        "while original before/after stay in source metadata. Their first acknowledgements record replay time; the ledger keeps the "
        "source effective time. Source, reviewer, money/advance/debts, rows, lineage and decision commit atomically. Matching known "
        "payments/allocations require their first receipt and immutable cash/part/ledger facts; later allocations never reset balances. "
        "Native payment histories project source devices and allocation actors after scope guards; historical UUIDs grant no rights. "
        "Purchase documents preserve original line snapshots and source UUIDs, including the initial invoice debt UUID. "
        "Order, lines and lineage create no financial or physical effect. An invoice adds only its full original debt and dated ledger. "
        "Document, lines, optional debt/ledger, origin and review commit atomically. Matching known creation receipts remain unchanged "
        "after partial receipts or debt allocations; source creation time stays in metadata and replay creation time in its acknowledgement. "
        "A matching already committed sale or collection is acknowledged with no second effect. "
        "Same review command/content returns its original receipt after current authorization; other content or stale context is 409. "
        "The business_receipt is the operation's first persisted acknowledgement, not its target's current state. "
        "A newly recovered deadline acknowledgement reflects the balance at replay, not a restored historical balance; "
        "retries keep that acknowledgement even after later payments or revisions. "
        "For a pending recovered-sale review, the same transaction replaces its reservation with the validated movement "
        "and records the linked review decision; the available stock is not deducted twice. A refused review requires "
        "separate explicit regularization before further integration. A pending collection review is validated in the same "
        "transaction as its money effects, with a linked audit decision. Other operation families, blocked-source review, "
        "financial refusal regularization and recovery closure remain to implement."))
    def integrate(self,request,*args,**kwargs):
        self.authorize()
        receipt,created=integrate_recovery_operation(shop_id=self.kwargs["shop_id"],actor=request.user,
            session_id=request.device_session.id,capture_id=self.kwargs["pk"],data=request.data)
        return Response(receipt,status=201 if created else 200)
