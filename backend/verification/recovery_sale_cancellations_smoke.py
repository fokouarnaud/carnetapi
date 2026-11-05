"""Restore actual T0, then recover two native cancellations, children and settlements."""
from verification.restore_installation_smoke import main

if __name__=='__main__':
    main(return_settlements=True,lost_returns=True,sale_cancellations=True)
