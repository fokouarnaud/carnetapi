"""Suspend archived credentials on a separate copy. Never activate a database."""
import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prepared_dir", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--requested-by", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    from backups.services.restore_quarantine import RestoreQuarantineError, quarantine_restore
    try:
        result = quarantine_restore(args.prepared_dir, args.output_dir, operation_id=args.operation_id,
            requested_by=args.requested_by, reason=args.reason)
    except RestoreQuarantineError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, RecursionError):
        print(json.dumps({"error": "restore_quarantine_failed"}), file=sys.stderr)
        return 1
    print(json.dumps({"candidate": str(result.path), **result.manifest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
