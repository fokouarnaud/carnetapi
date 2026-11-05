"""Replace actual T0, then recover lost native T1 returns and their settlements."""
from verification.restore_installation_smoke import main

if __name__=="__main__":
    main(return_settlements=True,lost_returns=True)
