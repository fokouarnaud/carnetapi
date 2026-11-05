"""Restore T0 and replay lost supplier documents, money and two physical arrivals."""
from verification.restore_installation_smoke import main

if __name__=='__main__':
    main(supplier_dependencies=True,supplier_payments=True,supplier_documents=True,purchase_receipts=True)
