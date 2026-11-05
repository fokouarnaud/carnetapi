"""Compatibility facade; business models are defined in domain apps."""
from common.model_base import (
    UUIDRecord,
)
from common.models import (
    Activity,
)
from identity.models import (
    User,
    LanguagePreferenceChange,
    Shop,
    Membership,
    Installation,
    DeviceSession,
    ShopDeviceAccess,
)
from catalogue.models import (
    Category,
    Product,
    ProductPhoto,
    ProductPack,
    Barcode,
    CatalogueImport,
)
from stock.models import (
    StockReservation,
    StockNotificationPreference,
    StockAlertState,
    StockAlertEvent,
    StockMovement,
    StockLoss,
    StockCount,
    StockCountAdjustment,
    ReturnReceipt,
    NonSellableDecision,
    StockShortageReview,
    StockValuationAdjustment,
    StockCostSnapshot,
    ReturnCostSnapshot,
    CostRevaluation,
    CostRevaluationItem,
    ReturnCostRevision,
    ReturnCostReconstruction,
)
from sales.models import (
    Sale,
    SaleLine,
    SalePayment,
    SaleReturn,
    SaleReturnLine,
    SaleCancellation,
)
from customers.models import (
    Customer,
    CustomerDebt,
    CustomerPayment,
    CustomerPaymentPart,
    DebtAllocation,
    PaymentRegularization,
    RegularizationAllocation,
    CustomerLedgerEntry,
    CustomerCredit,
    ReturnCreditSettlement,
    ReturnCreditAllocation,
)
from purchasing.models import (
    Supplier,
    PurchaseDocument,
    PurchaseLine,
    SupplierDebt,
    PurchaseReceipt,
    PurchaseReceiptLine,
    SupplierPayment,
    SupplierPaymentPart,
    SupplierPaymentAllocation,
    SupplierReturn,
    SupplierReturnLine,
    SupplierCredit,
    SupplierRefund,
    SupplierCreditAllocation,
    SupplierLedgerEntry,
    PurchaseCharge,
    PurchaseChargeRevision,
    PurchaseChargeAllocation,
)
from cash.models import (
    CashMovement,
    CashCount,
)
