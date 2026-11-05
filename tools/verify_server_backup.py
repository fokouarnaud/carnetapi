"""Authenticate encrypted server backup parts without starting Django.

This command only produces a verified snapshot. It never replaces a live
database, runs migrations or reinstates account/device permissions.
"""
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
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    from backups.services.archive import BackupArchiveError, verify_bundle
    try:
        if args.key_file.stat().st_size > 256 or args.expected_migrations.stat().st_size > 65536:
            raise BackupArchiveError("invalid_verification_configuration")
        key = args.key_file.read_bytes().strip()
        expected = json.loads(args.expected_migrations.read_text(encoding="utf-8"))
        if not isinstance(expected, list) or len(expected) > 2048 or any(
                not isinstance(row, list) or len(row) != 2 or any(not isinstance(item, str) or not 0 < len(item) <= 255 for item in row)
                for row in expected):
            raise BackupArchiveError("invalid_schema_manifest")
        result = verify_bundle(args.parts, key, args.output_dir, expected_migrations=expected)
    except BackupArchiveError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, RecursionError):
        print(json.dumps({"error": "backup_verification_failed"}), file=sys.stderr)
        return 1
    print(json.dumps({"backup_id": result.manifest["backup_id"], "snapshot": str(result.path),
        "snapshot_sha256": result.manifest["snapshot_sha256"], "known_migration_identifiers": True,
        "migration_execution_checked": False, "live_database_replaced": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
