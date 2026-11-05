"""Restore T0 and recover a native 30000 payment allocated to a 100000 opening debt."""
from verification.restore_installation_smoke import main

if __name__=='__main__':
    main(supplier_dependencies=True,supplier_payments=True)
