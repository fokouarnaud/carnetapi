"""Replace T0, recover lost native T1 returns, physical receipts and settlements."""
from verification.restore_installation_smoke import main

if __name__=='__main__':
    main(return_settlements=True,lost_returns=True,physical_returns=True)
