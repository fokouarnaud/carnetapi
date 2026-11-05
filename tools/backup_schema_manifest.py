"""Export known migration identifiers before an incident; no database is read."""
import argparse
import json
import os
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.test_settings"
    import django
    django.setup()
    from django.db.migrations.loader import MigrationLoader
    data = json.dumps(sorted(MigrationLoader(None).disk_migrations), indent=2) + "\n"
    if args.check:
        return 0 if args.output.is_file() and args.output.read_text(encoding="utf-8") == data else 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(data, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
