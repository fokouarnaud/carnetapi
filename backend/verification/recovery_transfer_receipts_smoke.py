"""Restore T0, revalidate access and recover a lost native partial transfer receipt."""
from verification.restore_installation_smoke import main


if __name__ == "__main__":
    main(transfer_receipts=True)
