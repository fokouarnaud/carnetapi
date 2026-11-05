"""Restore real T0 and recover missing supplier identity and three separate openings."""
from verification.restore_installation_smoke import main

if __name__=='__main__':
    main(supplier_dependencies=True)
