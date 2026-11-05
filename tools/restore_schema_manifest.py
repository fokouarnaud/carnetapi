"""Rebuild a trusted schema reference in an empty private database, never live data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory, NamedTemporaryFile
import uuid


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    worker = Path(__file__).resolve().parent.parent / "backend/backups/services/restore_migrations.py"
    try:
        with TemporaryDirectory(prefix="restore-work-") as directory:
            work = Path(directory).resolve()
            candidate = work / "candidate.sqlite3"
            candidate.touch()
            nonce = str(uuid.uuid4())
            (work / "request.json").write_text(json.dumps({"nonce": nonce, "purpose": "schema-reference",
                "snapshot_sha256": hashlib.sha256(b"").hexdigest()}), encoding="utf-8")
            environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "PATH") if key in os.environ}
            environment.update(TEMP=str(work), TMP=str(work), TMPDIR=str(work), LANG="C.UTF-8")
            completed = subprocess.run([sys.executable, "-I", "-X", "utf8", str(worker), str(work), nonce, "--schema-reference"],
                cwd=work, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=False)
            report = work / "worker_report.json"
            if completed.returncode or not report.is_file() or report.stat().st_size > 1000000:
                raise ValueError()
            reference = json.loads(report.read_text(encoding="utf-8"))
            if reference.get("format") != "app-x-sqlite-schema-reference":
                raise ValueError()
            data = json.dumps(reference, sort_keys=True, indent=2, ensure_ascii=True)+"\n"
        if args.check:
            if args.output.is_file() and args.output.read_text(encoding="utf-8") == data:
                print(json.dumps({"schema_reference": "current"}))
                return 0
            print(json.dumps({"error": "restore_schema_reference_stale"}), file=sys.stderr)
            return 1
        output = args.output.absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, prefix="schema-reference-", delete=False) as target:
                temporary = Path(target.name).resolve()
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            if temporary.parent != output.parent.resolve():
                raise ValueError()
            os.replace(temporary, output)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print(json.dumps({"schema_reference": "generated", "objects": len(reference["schema"]["objects"])}))
        return 0
    except (OSError, ValueError, TypeError, RecursionError, subprocess.TimeoutExpired):
        print(json.dumps({"error": "restore_schema_reference_generation_failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
