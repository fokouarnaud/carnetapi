"""A real invoice remains in T0; lost payment/allocation recover without a second outflow."""
from verification.restore_installation_smoke import main

if __name__=='__main__':
    main(supplier_dependencies=True,supplier_payments=True,supplier_invoice_in_t0=True)
