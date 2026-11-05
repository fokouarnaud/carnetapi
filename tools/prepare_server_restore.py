"""Verify encrypted parts and migrate a disposable copy. Never activate a database."""
import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parts", nargs="+", type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--expected-migrations", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    from backups.services.archive import BackupArchiveError
    from backups.services.restore_preparation import RestorePreparationError, prepare_restore
    try:
        if args.key_file.stat().st_size > 256 or args.expected_migrations.stat().st_size > 65536:
            raise RestorePreparationError("restore_configuration_invalid")
        result = prepare_restore(args.parts, args.key_file.read_bytes().strip(), args.output_dir,
            expected_migrations=json.loads(args.expected_migrations.read_text(encoding="utf-8")), timeout_seconds=args.timeout_seconds)
    except (BackupArchiveError, RestorePreparationError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, RecursionError):
        print(json.dumps({"error": "restore_preparation_failed"}), file=sys.stderr)
        return 1
    print(json.dumps({"candidate": str(result.path), **result.manifest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
