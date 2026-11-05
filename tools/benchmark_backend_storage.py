"""Measure a synthetic SQL storage floor in a disposable, isolated database."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    for name, default in (("shops", 2), ("products-per-shop", 1000), ("devices-per-shop", 3),
            ("sales-per-shop-day", 100), ("lines-per-sale", 5), ("days", 365)):
        parser.add_argument("--"+name, type=int, default=default)
    parser.add_argument("--max-database-bytes", type=int, default=3000000000)
    parser.add_argument("--host-limit-bytes", type=int)
    args = parser.parse_args(argv)
    repository = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repository / "backend"))
    from verification.storage_model import Workload
    try:
        target = Workload(**{name: getattr(args, name) for name in Workload.__dataclass_fields__})
        if args.max_database_bytes <= 0 or (args.host_limit_bytes is not None and args.host_limit_bytes <= 0):
            raise ValueError()
    except ValueError:
        print(json.dumps({"error": "invalid_storage_workload"}), file=sys.stderr)
        return 1
    try:
        with TemporaryDirectory(prefix="restore-work-") as directory:
            work = Path(directory).resolve()
            candidate = work / "candidate.sqlite3"
            candidate.touch()
            (work / "request.json").write_text(json.dumps({"purpose": "storage-floor", "workload": asdict(target),
                "max_database_bytes": args.max_database_bytes, "host_limit_bytes": args.host_limit_bytes}), encoding="utf-8")
            environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "PATH") if key in os.environ}
            environment.update(TEMP=str(work), TMP=str(work), TMPDIR=str(work), SQLITE_TMPDIR=str(work), LANG="C.UTF-8")
            measured = subprocess.run([sys.executable, "-I", "-X", "utf8", str(repository / "backend/verification/storage_floor_worker.py"), str(work)],
                cwd=work, env=environment, timeout=1800, check=False)
            if measured.returncode or not (work / "measurement.json").is_file():
                raise ValueError()
            report = json.loads((work / "measurement.json").read_text(encoding="utf-8"))
        report["temporary_database_removed"] = not candidate.exists()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
        print(json.dumps({"storage_floor": "measured", "database_bytes": report["database_bytes"],
            "actual_counts": report["actual_counts"], "target_row_counts_reached": report["target_row_counts_reached"],
            "report": str(args.output)}), flush=True)
        return 0
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        print(json.dumps({"error": "storage_floor_measurement_failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
