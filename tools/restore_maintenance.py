"""Control the external maintenance gate; no database activation or account creation."""
import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("initialize", "status", "enter", "abort", "upgrade"):
        command = commands.add_parser(action)
        command.add_argument("--gate", required=True, type=Path)
        if action in ("initialize","upgrade"):
            command.add_argument("--database", required=True, type=Path)
        if action != "status":
            command.add_argument("--operation-id", required=True)
            command.add_argument("--requested-by", required=True)
            command.add_argument("--reason", required=True)
        if action in ("enter", "abort","upgrade"):
            command.add_argument("--expected-version", required=True, type=int)
            command.add_argument("--timeout", default=0, type=float)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    from backups.services.restore_gate import (
        RestoreGateError, initialize_gate, read_gate, enter_maintenance, abort_maintenance, upgrade_gate,
    )
    try:
        if args.action == "status":
            result = read_gate(args.gate)
        else:
            identity = dict(operation_id=args.operation_id, requested_by=args.requested_by, reason=args.reason)
            if args.action == "initialize":
                args.gate.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                result = initialize_gate(args.gate, args.database, **identity)
            elif args.action=="upgrade":
                result=upgrade_gate(args.gate,args.database,**identity,expected_version=args.expected_version,timeout=args.timeout)
            else:
                transition = enter_maintenance if args.action == "enter" else abort_maintenance
                result = transition(args.gate, expected_version=args.expected_version,
                                    timeout=args.timeout, **identity)
    except RestoreGateError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError):
        print(json.dumps({"error": "restore_gate_failed"}), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
