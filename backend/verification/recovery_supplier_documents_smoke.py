"""Restore empty T0 and replay a lost order, invoice, payment and allocation."""
from verification.restore_installation_smoke import main

if __name__=='__main__':
    main(supplier_dependencies=True,supplier_payments=True,supplier_documents=True)
