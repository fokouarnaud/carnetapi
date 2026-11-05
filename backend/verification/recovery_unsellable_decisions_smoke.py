"""Restore actual T0, then recover eleven native T1 events including three dispositions."""
from verification.restore_installation_smoke import main

if __name__=='__main__':
    main(return_settlements=True,lost_returns=True,physical_returns=True,non_sellable_decisions=True)
